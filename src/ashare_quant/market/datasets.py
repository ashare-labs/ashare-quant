"""Pin existing AD exports and select scopes; never collect, infer limits or fill bars."""

import copy
import json
import tempfile
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.core.validation import digest as history_digest
from ashare_quant.market.contracts import (
    PortfolioDataset,
    PortfolioPreflight,
    PortfolioScope,
    PortfolioSelection,
)
from ashare_quant.portfolio.inputs import InputNumberError, Inputs
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, require, sha


def check_frozen_data(manifest, raw):
    """Cross-check existing frozen identities; never reopen the mutable catalogue."""
    request, report = manifest.request, manifest.preflight
    selection = PortfolioSelection.model_validate(
        {name: getattr(request, name) for name in PortfolioSelection.model_fields}
    )
    # v1's acknowledgement predates the default historical_pit_verified field.
    acknowledged = report.model_dump(
        mode="json", exclude={"assumption_ack_sha256", "historical_pit_verified"}
    )
    require(
        report.ready
        and not report.gaps
        and selection == report.selection
        and request.assumption_ack_sha256 == report.assumption_ack_sha256 == digest(acknowledged),
        "PORTFOLIO_FROZEN_DATA_CONFLICT",
        "Frozen request selection or acknowledgement differs from its accepted preflight",
    )
    document = json.loads(raw)
    descriptor, session = manifest.data_identity, manifest.session
    require(
        selection.dataset_id == descriptor.dataset_id
        and descriptor.dataset_id
        == digest({"name": descriptor.name, "input_sha256": descriptor.input_sha256})
        and document["source_projection"]
        == {
            "dataset_id": descriptor.dataset_id,
            "original_input_sha256": descriptor.input_sha256,
        }
        and sha(raw)
        == manifest.files["input.json"]
        == report.input_sha256
        == session["details"]["input_sha256"]
        and session["run_id"] == manifest.job_id.removeprefix("m2job-")
        and session["native_identity"] == history_digest(session["details"]),
        "PORTFOLIO_FROZEN_DATA_CONFLICT",
        "Frozen source, projected input or native session identity differs",
    )
    start, end = str(selection.start_date), str(selection.end_date)
    days = [day for day in document["calendar"] if start <= day <= end]
    diagnostic = selection.mode == "price_only_no_limits"
    require(
        report.scope
        == manifest.execution_scope
        == PortfolioScope(
            securities=selection.securities,
            start_date=start,
            end_date=end,
            trading_dates=days,
        )
        and document["securities"] == list(selection.securities)
        and set(selection.securities) <= set(descriptor.securities)
        and set(days) <= {str(day) for day in descriptor.trading_dates}
        and report.economic_scope
        == (
            "price_only_no_limits_not_total_return"
            if diagnostic
            else "source_covered_conditional_research"
        )
        and diagnostic
        == (document.get("price_limit_policy") == "disabled_for_price_only_diagnostic")
        and diagnostic == (document.get("research_model") == "price_only_no_actions")
        and document["assumptions"] == list(report.assumptions)
        and document["limitations"] == list(report.limitations)
        and document["real_market_data"] is True
        and report.real_market_data
        and descriptor.real_market_data
        and (
            document.get("state_mode") != "retrospective_model"
            or selection.accept_retrospective_state
        ),
        "PORTFOLIO_FROZEN_DATA_CONFLICT",
        "Frozen execution scope or research mode differs from its input and preflight",
    )


def validate(document, selection):
    """Use the exact executor preflight even when merely inspecting a catalogue."""
    manifest = {"securities": list(selection.securities)}
    window = {"id": "selected", "start": str(selection.start_date), "end": str(selection.end_date)}
    with tempfile.TemporaryDirectory(prefix="aq-portfolio-preflight-") as folder:
        path = Path(folder) / "input.json"
        path.write_bytes(canonical(document))
        return Inputs(
            path,
            manifest,
            window,
            accept_price_only_research=selection.mode == "price_only_no_limits",
        )


class Datasets:
    def __init__(self, root):
        self.root = root / "portfolio-data"

    def bounded_file(self, path):
        # Compatibility name; source exports have no unrelated per-file ceiling.
        return regular(path)

    def read(self, dataset_id):
        try:
            return self._read(dataset_id)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise DomainError(
                "PORTFOLIO_DATASET_CONFLICT",
                "Dataset descriptor or input is malformed or missing",
                409,
            ) from exc

    def _read(self, dataset_id):
        from pydantic import TypeAdapter

        from ashare_quant.core.types import Sha

        TypeAdapter(Sha).validate_python(dataset_id)
        require(not self.root.is_symlink())
        root = self.root / dataset_id
        require(not root.is_symlink())
        if not root.exists():
            raise DomainError(
                "PORTFOLIO_DATASET_NOT_FOUND", "Import this immutable AD export locally", 404
            )
        declared = json.loads(regular(root / "descriptor.json"))
        descriptor = PortfolioDataset.model_validate(declared)
        raw = self.bounded_file(root / "input.json")
        require(sha(raw) == descriptor.input_sha256 and descriptor.dataset_id == dataset_id)
        require(dataset_id == digest({"name": descriptor.name, "input_sha256": sha(raw)}))
        doc = json.loads(raw)
        current = self.describe(doc, descriptor.name, raw)
        # Older descriptors lack these derived display fields; preserve their bytes
        # and content identity while exposing modes from the actual frozen input.
        compatible = current.model_copy(update={k: getattr(descriptor, k)
            for k in ("mode", "diagnostic_allowed") if k not in declared})
        require(descriptor == compatible)
        return current, doc

    @staticmethod
    def describe(doc, name, raw):
        from ashare_quant.portfolio.product_reference import validate

        validate(doc)
        require(
            doc["schema"] == "ad-daily-portfolio.v1",
            "PORTFOLIO_DATA_SCHEMA",
            "Use the existing AD daily portfolio export",
        )
        require(
            doc["producer"]["name"] == "ashare-data" and doc["real_market_data"] is True,
            "PORTFOLIO_REAL_DATA_REQUIRED",
            "The public task service accepts real AD exports only",
        )
        require(
            doc.get("owner_api_reads") and doc.get("owner_history_sha256"),
            "PORTFOLIO_OWNER_EVIDENCE_REQUIRED",
            "Preserve public AD read receipts and history digests",
        )
        return PortfolioDataset(
            dataset_id=digest({"name": name, "input_sha256": sha(raw)}),
            name=name,
            input_sha256=sha(raw),
            securities=tuple(sorted(doc["securities"])),
            trading_dates=tuple(sorted(doc["owner_history_sha256"])),
            real_market_data=True,
            state_mode=doc.get("state_mode", "unknown"),
            action_coverage=doc["action_coverage"],
            source_refs=doc["source_refs"],
            limitations=doc["limitations"],
            mode=("price_only_no_limits" if doc.get("research_model") == "price_only_no_actions"
                  else "covered_research"),
            diagnostic_allowed="corporate_schedule" not in doc,
        )

    def import_file(self, path, name):
        require(
            bool(name.strip()) and len(name) <= 80,
            "PORTFOLIO_DATA_NAME",
            "Use a short dataset name",
        )
        raw = self.bounded_file(path)
        try:
            descriptor = self.describe(json.loads(raw), name, raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "PORTFOLIO_DATA_INVALID",
                "Use a complete AD daily portfolio export with source receipts",
                422,
            ) from exc
        require(not self.root.is_symlink())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock(self.root / "import.lock", timeout=2):
            root = self.root / descriptor.dataset_id
            require(not root.is_symlink())
            root.mkdir(exist_ok=True, mode=0o700)
            if (root / "descriptor.json").exists():
                return self.read(descriptor.dataset_id)[0]
            atomic(root / "input.json", raw)
            atomic(root / "descriptor.json", descriptor)
        return descriptor

    def list(self):
        require(not self.root.is_symlink())
        return tuple(
            self.read(p.parent.name)[0] for p in sorted(self.root.glob("*/descriptor.json"))
        )

    def select(self, selection):
        descriptor, original = self.read(selection.dataset_id)
        doc = copy.deepcopy(original)
        gaps, scope = [], None
        start, end = str(selection.start_date), str(selection.end_date)
        assumptions = list(doc.get("assumptions", []))
        limitations = list(doc["limitations"])
        economic = "source_covered_conditional_research"
        if selection.mode == "price_only_no_limits":
            economic = "price_only_no_limits_not_total_return"
            # Preserve upstream statements as provenance, not as active execution rules.
            assumptions = [
                {
                    "id": "source_export_assumptions",
                    "scope": "source_provenance_only",
                    "text": "These describe the original export, not this diagnostic execution; price limits and corporate actions are excluded.",
                    "original_assumptions": assumptions,
                },
                "Corporate actions and price limits are excluded from this diagnostic book; it is not a complete economic backtest.",
            ]
            limitations += [
                "No price-limit constraint; no corporate-action cash or shares; not total return."
            ]
        try:
            ids = set(selection.securities)
            unsupported = [
                s
                for s in ids
                if not (
                    s.endswith(".XSHG")
                    and s[:3] in {"600", "601", "603", "605"}
                    or s.endswith(".XSHE")
                    and s[:3] in {"000", "001", "002", "003"}
                )
            ]
            require(
                not unsupported,
                "PORTFOLIO_INSTRUMENT_UNSUPPORTED",
                "Native contract supports ordinary main-board A shares: " + ",".join(unsupported),
            )
            require(
                ids <= set(doc["securities"]),
                "PORTFOLIO_SECURITY_UNAVAILABLE",
                "Requested securities are not in this dataset",
            )
            calendar = doc["calendar"]
            require(calendar == sorted(set(calendar)), "PORTFOLIO_CALENDAR_INVALID")
            require(
                start in calendar and end in calendar,
                "PORTFOLIO_DATE_UNAVAILABLE",
                "Endpoints must be observed exchange sessions",
            )
            first, last = calendar.index(start), calendar.index(end)
            require(
                first >= 120 and last + 1 < len(calendar),
                "PORTFOLIO_WARMUP_UNAVAILABLE",
                "120 warmup sessions and a successor session are required",
            )
            days = calendar[first : last + 1]
            require(
                set(days) <= set(doc["owner_history_sha256"]),
                "PORTFOLIO_HISTORY_UNAVAILABLE",
                "This dataset has no owner history receipts for the requested sessions",
            )
            rows = {(r["security"], r["date"]): r for r in doc["daily"]}
            require(len(rows) == len(doc["daily"]), "PORTFOLIO_DUPLICATE_ROW")
            # First verify the unchanged owner universe. A narrowed universe gets a
            # new derived hash only after its original source receipt is verified.
            for index in range(first, last + 1):
                day = calendar[index]
                history = {
                    sid: [
                        rows.get((sid, d), {}).get("close") for d in calendar[index - 120 : index]
                    ]
                    for sid in doc["securities"]
                }
                require(
                    history_digest(history) == doc["owner_history_sha256"][day],
                    "PORTFOLIO_HISTORY_CHANGED",
                    "Owner history receipt does not match the supplied observations",
                )
            doc["source_projection"] = {
                "dataset_id": descriptor.dataset_id,
                "original_input_sha256": descriptor.input_sha256,
            }
            doc["securities"] = list(selection.securities)
            doc["calendar"] = calendar[first - 120 : last + 2]
            doc["daily"] = [
                r
                for r in doc["daily"]
                if r["security"] in ids and doc["calendar"][0] <= r["date"] <= end
            ]
            doc["actions"] = [
                a
                for a in doc.get("actions", [])
                if a["security"] in ids and start <= a["ex_date"] <= end
            ]
            doc["additional_gaps"] = [
                g
                for g in doc.get("additional_gaps", [])
                if ("security" not in g or g["security"] in ids)
                and ("date" not in g or start <= g["date"] <= end)
            ]
            doc["owner_history_sha256"] = {
                d: history_digest(
                    {
                        s: [
                            rows.get((s, p), {}).get("close")
                            for p in calendar[calendar.index(d) - 120 : calendar.index(d)]
                        ]
                        for s in selection.securities
                    }
                )
                for d in days
            }
            if selection.mode == "price_only_no_limits":
                require(
                    "corporate_schedule" not in doc,
                    "PORTFOLIO_CORPORATE_PRICE_ONLY_CONFLICT",
                    "A formal corporate schedule cannot be silently discarded by a price-only selection",
                )
                doc.update(
                    actions=[],
                    action_coverage={"status": "unavailable"},
                    research_model="price_only_no_actions",
                    price_limit_policy="disabled_for_price_only_diagnostic",
                    economic_acceptance="price_only_diagnostic_not_economic_acceptance",
                    state_mode="retrospective_model",
                )
                # Missing values stay missing; no 5%/10% prices are manufactured.
                for row in doc["daily"]:
                    row["upper_limit"] = row["lower_limit"] = None
                doc["additional_gaps"] = [
                    g
                    for g in doc["additional_gaps"]
                    if g.get("field")
                    not in {
                        "corporate_actions",
                        "cash_payment_date",
                        "share_credit_or_tradable_date",
                        "upper_limit",
                        "lower_limit",
                    }
                ]
            else:
                # An AQ-derived limit cannot silently enter the formal covered path.
                for row in doc["daily"]:
                    if row["date"] >= start and row.get("limit_origin", "").startswith("AQ "):
                        gaps.append(
                            {
                                "field": "observed_price_limits",
                                "security": row["security"],
                                "date": row["date"],
                                "detail": row["limit_origin"],
                            }
                        )
            doc["assumptions"], doc["limitations"] = assumptions, limitations
            data = validate(doc, selection)
            if data.corporate_actions is not None:
                from ashare_quant.portfolio.corporate import new_account_assumption

                # Public jobs have no initial-position/receivable import. Bind this
                # account policy and the exact selected events into the existing ack.
                assumptions = [a for a in assumptions if not (
                    isinstance(a, dict) and a.get("id") == "corporate_initial_entitlements"
                )]
                assumptions.append(new_account_assumption(data.corporate_actions, start))
                doc["assumptions"] = assumptions
            gaps += data.preflight()["gaps"]
            if (
                doc.get("state_mode") == "retrospective_model"
                and not selection.accept_retrospective_state
            ):
                gaps.append(
                    {
                        "field": "retrospective_state_opt_in",
                        "detail": "Explicit acceptance of the declared state model is required",
                    }
                )
            scope = PortfolioScope(
                securities=selection.securities, start_date=start, end_date=end, trading_dates=days
            )
        except InputNumberError as exc:
            gaps.append(exc.gap)
        except (DomainError, ValueError, KeyError, TypeError, IndexError) as exc:
            gaps.append(
                {
                    "field": "input_contract",
                    "code": getattr(exc, "code", "PORTFOLIO_INPUT_INVALID"),
                    "detail": str(exc)[:500],
                }
            )
        body = dict(
            selection=selection,
            ready=not gaps,
            gaps=tuple(gaps),
            assumptions=tuple(assumptions),
            limitations=tuple(limitations),
            economic_scope=economic,
            real_market_data=True,
            input_sha256=sha(canonical(doc)) if scope else None,
            scope=scope,
        )
        serializable = {
            **body,
            "selection": selection.model_dump(mode="json"),
            "scope": scope.model_dump(mode="json") if scope else None,
        }
        return PortfolioPreflight(**body, assumption_ack_sha256=digest(serializable)), doc
