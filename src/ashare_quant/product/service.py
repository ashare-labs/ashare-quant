"""Use the installed AD public API; never query or modify its source database."""

import sqlite3
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.market.reader import verify_data_runtime, wire
from ashare_quant.product.contracts import ProductConfiguration, ProductRelease
from ashare_quant.providers.runtime import pin
from ashare_quant.storage.artifacts import digest

LIMITATIONS = (
    "Prices are raw observed market data; unavailable observations are never replaced by sample data.",
    "Ordinary research requires the selected reference set to cover every requested security/day; "
    "configured references do not establish whole-library coverage or historical PIT.",
    "Price-only diagnosis is an explicit separate choice and excludes corporate actions and limits.",
    "Execution requires acceptance of the exact preflight, including retrospective state, "
    "source-scoped corporate coverage, date-level gross cash/share credit and fixed fees.",
    "The portfolio engine supports ordinary main-board A shares and requires 120 observed "
    "warmup sessions and a successor session.",
    "Fixed daily evaluations remain a separate explicit data binding.",
)


def environment_configuration():
    from ashare_quant.deployment import resolve

    return resolve()


class Product:
    def __init__(self, store, jobs, configuration=None):
        self.store, self.jobs = store, jobs
        self.configuration = configuration
        self.configuration_error = None
        if configuration is None:
            try:
                self.configuration = environment_configuration()
            except DomainError as exc:
                self.configuration_error = exc

    def configured(self):
        if self.configuration_error:
            raise self.configuration_error
        config = self.configuration
        if config is None or config.kind != "product":
            raise DomainError(
                "PRODUCT_NOT_CONFIGURED", "Select a product profile in the deployment manifest", 503
            )
        if Path(config.control_db).resolve() != self.store.path.resolve():
            raise DomainError(
                "PRODUCT_STATE_MISMATCH",
                "The API state database differs from the selected product profile; "
                "start the API and task worker with the same deployment and profile",
                409,
            )
        if config.daily_root is None or config.release_store_root is None:
            raise DomainError(
                "PRODUCT_NOT_CONFIGURED", "Configure a daily asset and publication Store", 503
            )
        return config

    def owner(self):
        config = self.configured()
        verify_data_runtime()
        try:
            from ashare_data.product import LocalDailyProduct
        except ImportError as exc:
            raise DomainError(
                "PRODUCT_OWNER_API_UNAVAILABLE",
                "Install the pinned AD candidate that provides LocalDailyProduct",
                503,
            ) from exc
        return LocalDailyProduct(config.daily_root)

    def call(self, operation):
        try:
            from ashare_data import DataError
        except ImportError as exc:
            raise DomainError(
                "PRODUCT_OWNER_API_UNAVAILABLE", "Install the pinned AD candidate", 503
            ) from exc

        try:
            return operation(self.owner())
        except DataError as exc:
            raise DomainError(
                exc.code, exc.message, 422, details=wire(exc.details), origin="ashare-data"
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise DomainError(
                "PRODUCT_DATA_UNAVAILABLE",
                "The configured market asset or publication Store is unavailable",
                503,
                details={"failure": type(exc).__name__},
            ) from exc

    def describe(self):
        config = self.configuration
        error = None
        try:
            self.call(lambda owner: None)
        except DomainError as exc:
            error = exc.body()
        assets = ()
        configuration_id = None
        if config is not None:
            paths = {
                "data_root": str(config.data_root),
                "control_db": str(config.control_db),
                "daily_root": str(config.daily_root),
                "release_store_root": str(config.release_store_root),
            }
            configuration_id = digest({
                "profile": config.profile, "kind": config.kind, **paths,
                "default_reference_set": config.default_reference_set,
                "reference_sets": [item.references() for item in config.reference_sets],
            })
            assets = tuple({
                "name": item.name, "kind": item.kind, "role": item.role,
                "path": str(item.path), "retention": item.retention,
                "read_only": item.path != config.release_store_root,
                "selected": item.name in config.selected_assets,
                **({"binding": item.binding, "binding_sha256": item.binding_sha256}
                   if item.binding is not None else {}),
            } for item in config.assets) + (
                {"kind": "sqlite", "role": "task_state", "path": paths["control_db"], "read_only": False},
            )
        owner_pin = pin()
        return ProductConfiguration(
            profile=config.profile if config is not None else "product",
            kind=config.kind if config is not None else None,
            configured=error is None,
            configuration_id=configuration_id,
            default_dataset_id=config.default_dataset_id if config is not None else None,
            assets=assets,
            capabilities={
                "catalogue": error is None,
                "prices": error is None,
                "publish": error is None,
                "portfolio": error is None,
                "covered_research": error is None and bool(config and config.reference_sets),
                "price_only_no_limits": error is None,
                "fixed_evaluation": "separate_explicit_binding",
                "portfolio_instrument_scope": "main_board_ordinary_a_shares",
                "warmup_sessions": 120,
                "preflight_required": True,
            },
            limitations=LIMITATIONS,
            reference_sets=tuple({"name": item.name, "kind": item.kind,
                                  "source_bindings": item.source_bindings()}
                                 for item in config.reference_sets) if config else (),
            default_reference_set=config.default_reference_set if config else None,
            data_owner={key: owner_pin[key] for key in (
                "version", "commit", "tree", "runtime_code_sha256", "wheel_sha256"
            )},
            error=error,
        )

    def catalogue(self, query, limit, offset):
        return self.call(lambda owner: wire(owner.catalogue(query=query, limit=limit, offset=offset)))

    def prices(self, selection):
        return self.call(lambda owner: wire(owner.prices(
            list(selection.securities), str(selection.start), str(selection.end),
            source=selection.source,
        )))

    def coverage(self, selection):
        from ashare_quant.product.publication import check_coverage

        references = self.configured().references(selection.reference_set)
        value = self.call(lambda owner: wire(self.operation(owner, "coverage")(
            list(selection.securities), str(selection.start), str(selection.end),
            source=selection.source, references=references,
        )))
        check_coverage(value, selection, references)
        return value

    @staticmethod
    def operation(owner, name):
        method = getattr(owner, name, None)
        if method is None:
            raise DomainError("PRODUCT_OWNER_API_UNAVAILABLE",
                              "Install the pinned AD product interface", 503)
        return method

    def publish(self, selection):
        from ashare_quant.product.publication import consume

        config = self.configured()
        self.jobs.guard()
        references = config.references(selection.reference_set)
        unsupported = [sid for sid in selection.securities if not (
            sid.endswith(".XSHG") and sid[:3] in {"600", "601", "603", "605"}
            or sid.endswith(".XSHE") and sid[:3] in {"000", "001", "002", "003"}
        )]
        if unsupported:
            raise DomainError(
                "PORTFOLIO_INSTRUMENT_UNSUPPORTED",
                "Portfolio publication supports ordinary main-board A shares",
                422, details={"securities": unsupported},
            )
        release = self.call(lambda owner: wire(self.operation(owner, "publish")(
            config.release_store_root, list(selection.securities), str(selection.start),
            str(selection.end), source=selection.source, mode=selection.mode, references=references,
        )))
        path = self.call(lambda owner: consume(release, selection, config, references))
        name = selection.name or f"Market {selection.start} to {selection.end}"
        dataset = self.jobs.import_data(path, name)
        return ProductRelease(release=release, dataset=dataset)
