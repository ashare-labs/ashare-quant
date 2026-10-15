"""Pin existing AD exports and select scopes; never collect, infer limits or fill bars."""

import copy
import json
import tempfile
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.daily_portfolio.inputs import Inputs
from ashare_quant.daily_portfolio.inputs import digest as history_digest
from ashare_quant.m2_jobs.files import atomic, canonical, digest, lock, regular, require, sha

from .contracts import PortfolioDataset, PortfolioPreflight, PortfolioScope


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
    def __init__(self, root, file_bytes=67108864):
        self.root = root / "portfolio-data"
        self.file_bytes = file_bytes

    def bounded_file(self, path):
        if Path(path).stat().st_size > self.file_bytes:
            raise DomainError(
                "PORTFOLIO_INPUT_RESOURCE_LIMIT",
                "Export exceeds this service's file_bytes budget",
                422,
                details={"file_bytes": self.file_bytes},
            )
        raw = regular(path)
        require(len(raw) <= self.file_bytes, "PORTFOLIO_INPUT_RESOURCE_LIMIT")
        return raw

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

        from ashare_quant.m2.contracts import Sha

        TypeAdapter(Sha).validate_python(dataset_id)
        require(not self.root.is_symlink())
        root = self.root / dataset_id
        require(not root.is_symlink())
        if not root.exists():
            raise DomainError(
                "PORTFOLIO_DATASET_NOT_FOUND", "Import this immutable AD export locally", 404
            )
        descriptor = PortfolioDataset.model_validate_json(regular(root / "descriptor.json"))
        raw = self.bounded_file(root / "input.json")
        require(sha(raw) == descriptor.input_sha256 and descriptor.dataset_id == dataset_id)
        require(dataset_id == digest({"name": descriptor.name, "input_sha256": sha(raw)}))
        doc = json.loads(raw)
        require(descriptor == self.describe(doc, descriptor.name, raw))
        return descriptor, doc

    @staticmethod
    def describe(doc, name, raw):
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
            assumptions += [
                "Corporate actions and price limits are excluded from this diagnostic book; it is not a complete economic backtest."
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
