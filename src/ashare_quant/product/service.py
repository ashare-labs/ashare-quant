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
    "This local asset does not establish complete historical corporate actions or price limits.",
    "Portfolio execution requires explicit price_only_no_limits and retrospective-state acceptance, "
    "then acknowledgement of the exact preflight. It excludes corporate actions and price limits "
    "and is not a total-return backtest.",
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
            configuration_id = digest({"profile": config.profile, "kind": config.kind, **paths})
            assets = tuple({
                "name": item.name, "kind": item.kind, "role": item.role,
                "path": str(item.path), "retention": item.retention,
                "read_only": item.path != config.release_store_root,
                "selected": item.name in config.selected_assets,
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
                "covered_research": False,
                "price_only_no_limits": error is None,
                "fixed_evaluation": "separate_explicit_binding",
                "portfolio_instrument_scope": "main_board_ordinary_a_shares",
                "warmup_sessions": 120,
                "preflight_required": True,
            },
            limitations=LIMITATIONS,
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

    def publish(self, selection):
        config = self.configured()
        self.jobs.guard()
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
        release = self.call(lambda owner: wire(owner.publish(
            config.release_store_root, list(selection.securities), str(selection.start),
            str(selection.end), source=selection.source,
        )))
        try:
            path = Path(release["portfolio_input_path"])
            if not path.is_absolute() or not path.resolve().is_relative_to(
                Path(config.release_store_root).resolve()
            ):
                raise ValueError("Input is outside the configured publication Store")
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                "PRODUCT_OWNER_CONTRACT_MISMATCH",
                "AD publication did not return an input inside the configured Store", 409,
            ) from exc
        name = selection.name or f"Market {selection.start} to {selection.end}"
        dataset = self.jobs.import_data(path, name)
        return ProductRelease(release=release, dataset=dataset)
