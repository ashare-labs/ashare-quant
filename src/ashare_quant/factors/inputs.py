"""Read explicitly registered frozen inputs; never discover, collect or repair data."""

import json
import os
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.factors.evaluation_inputs import market_labels, snapshot_labels, source_basis
from ashare_quant.storage.artifacts import canonical, digest, regular, require, sha

MAX_BYTES = 32 * 1024 * 1024
MAX_CELLS = 100_000
SCHEMAS = {
    "alpha191.panel.v1": "alpha191",
    "ad.factor-panel-batch-inputs.v1": "joinquant_panel",
    "ad.factor-research-independent-inputs.v1": "joinquant_research",
}


def validate_panel(payload):
    sessions, securities = payload["sessions"], payload["securities"]
    require(
        len(set(securities)) == len(securities),
        "FACTOR_SECURITY_SCOPE",
        "Duplicate source securities",
    )
    require(
        all(isinstance(d, str) and date.fromisoformat(d).isoformat() == d for d in sessions),
        "FACTOR_SESSION_GRID",
        "Canonical session dates required",
    )
    for name, matrix in {"universe": payload["universe"], **payload["fields"]}.items():
        require(
            isinstance(matrix, list)
            and len(matrix) == len(sessions)
            and all(isinstance(row, list) and len(row) == len(securities) for row in matrix),
            "FACTOR_SOURCE_INPUT",
            "Panel axes differ: " + name,
        )
        for row in matrix:
            for cell in row:
                if name == "universe":
                    require(
                        type(cell) is bool,
                        "FACTOR_SOURCE_INPUT",
                        "Membership cells must be explicit booleans",
                    )
                else:
                    require(
                        cell is None
                        or (type(cell) in (int, float, str) and Decimal(str(cell)).is_finite()),
                        "FACTOR_SOURCE_INPUT",
                        "Numeric cells must be finite or null",
                    )


def bounded_json(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_BYTES, "FACTOR_INPUT_LIMIT", "Input exceeds 32 MiB")
    raw = regular(path)
    require(len(raw) <= MAX_BYTES, "FACTOR_INPUT_LIMIT", "Input exceeds 32 MiB")
    return raw, json.loads(raw)


class Sources:
    def __init__(self, specifications=None, product=None):
        if specifications is None:
            path = os.environ.get("ASHARE_QUANT_FACTOR_SOURCES")
            specifications = bounded_json(path)[1] if path else {}
        require(isinstance(specifications, dict), "FACTOR_SOURCE_CONFIG", "Use a source mapping")
        self.specifications = deepcopy(specifications)
        self.product = product

    def definitions(self):
        result = deepcopy(self.specifications)
        if self.product is not None:
            try:

                def release(owner):
                    item = getattr(owner, "release", None)
                    return (
                        dict(
                            root=str(item.root),
                            release_id=item.release_id,
                            manifest_sha256=item.sha256,
                        )
                        if item
                        else None
                    )

                selected = self.product.call(release)
                if selected:
                    result["market-release"] = dict(
                        label="当前正式日线快照", kind="market_release", market_release=selected
                    )
            except DomainError:
                # Independent frozen samples still work without a product deployment.
                pass
        return result

    def identity(self):
        return digest(self.definitions())

    @staticmethod
    def market(spec):
        from ashare_data.product.market_release import ReleasedDailyProduct

        return ReleasedDailyProduct({"market_release": spec["market_release"]})

    def load(self, source_id):
        spec = self.definitions().get(source_id)
        require(spec is not None, "FACTOR_SOURCE_NOT_FOUND", "Select a registered frozen source")
        if spec["kind"] == "market_release":
            return spec, None
        raw, payload = bounded_json(spec["path"])
        require(
            sha(raw) == spec["sha256"], "FACTOR_SOURCE_CHANGED", "Registered input bytes changed"
        )
        require(
            SCHEMAS.get(payload.get("schema")) == spec["kind"],
            "FACTOR_SOURCE_SCHEMA",
            "Source schema differs from its registration",
        )
        if spec["kind"] == "joinquant_research" and spec.get("evidence_root"):
            from ashare_data import FactorResearchView

            FactorResearchView.from_file(
                spec["path"], evidence_root=spec["evidence_root"], expected_sha256=spec["sha256"]
            )
        return spec, payload

    def catalogue(self):
        rows = []
        for source_id, definition in self.definitions().items():
            base = dict(
                id=source_id, label=definition.get("label", source_id), kind=definition.get("kind")
            )
            try:
                spec, payload = self.load(source_id)
                if payload is None:
                    owner = self.market(spec)
                    listing = owner.catalogue(limit=500)
                    stocks = listing["securities"]
                    securities = [s["security"] for s in stocks]
                    first = min((s["first_bar"] for s in stocks if s["first_bar"]), default=None)
                    last = max((s["last_bar"] for s in stocks if s["last_bar"]), default=None)
                    capabilities = ["alpha191"]
                    fields = ["open", "high", "low", "close", "volume", "amount", "vwap"]
                    identity = owner.release.receipt()
                    total = listing["total"]
                else:
                    securities = payload.get(
                        "securities", [i["security"] for i in payload.get("items", [])]
                    )
                    first, last = payload["sessions"][0], payload["sessions"][-1]
                    capabilities = [spec["kind"]]
                    fields = list(payload.get("fields", {}))
                    identity = {
                        "sha256": spec["sha256"],
                        "provenance": payload.get("provenance", {}),
                    }
                    total = len(securities)
                rows.append(
                    dict(
                        **base,
                        ready=True,
                        securities=securities,
                        total_securities=total,
                        first=first,
                        last=last,
                        capabilities=capabilities,
                        fields=fields,
                        evaluation_basis=source_basis(spec, payload),
                        identity=identity,
                        error=None,
                    )
                )
            except (ValueError, KeyError, OSError, DomainError) as exc:
                rows.append(
                    dict(
                        **base,
                        ready=False,
                        securities=[],
                        total_securities=0,
                        first=None,
                        last=None,
                        capabilities=[],
                        fields=[],
                        identity={},
                        error=str(exc),
                    )
                )
            except Exception as exc:
                from ashare_data import DataError

                if not isinstance(exc, DataError):
                    raise
                rows.append(
                    dict(
                        **base,
                        ready=False,
                        securities=[],
                        total_securities=0,
                        first=None,
                        last=None,
                        capabilities=[],
                        fields=[],
                        identity={},
                        error=exc.message,
                    )
                )
        return rows

    def freeze(self, request):
        from ashare_data import DataError

        try:
            spec, payload = self.load(request.source_id)
            if payload is None:
                require(
                    request.factor_id.startswith("alpha191:"),
                    "FACTOR_INPUT_CONTRACT",
                    "This raw market source supplies Alpha191 panels. Select a JoinQuant input snapshot for its adjusted-price/share/financial contracts.",
                )
                payload, receipt, evaluation_inputs = market_panel(self.market(spec), spec, request)
                kind = "alpha191"
            else:
                payload = deepcopy(payload)
                receipt = dict(
                    source_sha256=spec["sha256"],
                    input_schema=payload["schema"],
                    source_files_verified=bool(spec.get("evidence_root")),
                )
                kind = spec["kind"]
                evaluation_inputs = None
                if kind == "alpha191":
                    require(
                        request.factor_id.startswith("alpha191:"),
                        "FACTOR_INPUT_CONTRACT",
                        "Select a JoinQuant input snapshot for this factor",
                    )
                else:
                    from ashare_data.factors.joinquant import panel, research_view

                    allowed = panel.FIELDS if kind == "joinquant_panel" else research_view.FIELDS
                    require(
                        request.factor_id in {"joinquant:" + f for f in allowed},
                        "FACTOR_INPUT_CONTRACT",
                        "The selected snapshot does not contain this factor's input contract",
                    )
            sessions = payload["sessions"]
            require(
                sessions and sessions == sorted(set(sessions)),
                "FACTOR_SESSION_GRID",
                "Use an explicit ordered session grid",
            )
            lo, hi = str(request.history_start), str(request.label_end or request.end_date)
            indices = [i for i, day in enumerate(sessions) if lo <= day <= hi]
            selected = [sessions[i] for i in indices]
            require(
                selected
                and any(str(request.start_date) <= d <= str(request.end_date) for d in selected),
                "FACTOR_EMPTY_WINDOW",
                "No declared trading sessions in the selected interval",
            )
            require(
                len(selected) * len(request.securities) <= MAX_CELLS,
                "FACTOR_INPUT_LIMIT",
                "Narrow the selection to at most 100000 security sessions",
            )
            if kind == "alpha191":
                validate_panel(payload)
                securities = payload["securities"]
                require(
                    set(request.securities) <= set(securities),
                    "FACTOR_SECURITY_SCOPE",
                    "Securities outside the selected snapshot",
                )
                columns = [securities.index(s) for s in request.securities]
                for key in ("universe",):
                    payload[key] = [[payload[key][i][j] for j in columns] for i in indices]
                payload["fields"] = {
                    f: [[a[i][j] for j in columns] for i in indices]
                    for f, a in payload["fields"].items()
                }
                for key in ("benchmark", "factors"):
                    if payload.get(key):
                        payload[key] = {f: [a[i] for i in indices] for f, a in payload[key].items()}
                payload["securities"] = list(request.securities)
            else:
                require(
                    set(request.securities) <= {r["security"] for r in payload["items"]},
                    "FACTOR_SECURITY_SCOPE",
                    "Securities outside the selected snapshot",
                )
                payload["items"] = [
                    r for r in payload["items"] if r["security"] in request.securities
                ]
                payload["securities"] = list(request.securities)
            payload["sessions"] = selected
            if request.evaluation is not None and evaluation_inputs is None:
                evaluation_inputs = snapshot_labels(payload, kind)
            frozen = dict(
                kind=kind,
                payload=payload,
                evaluation_inputs=evaluation_inputs,
                provenance={
                    "source_id": request.source_id,
                    "source_label": spec.get("label", request.source_id),
                    "receipt": receipt,
                    "history_start": selected[0],
                    "history_end": selected[-1],
                    "historical_pit_verified": False,
                    "network_used": False,
                    "universe_policy": "explicit_fixed_selection; missing observations retained",
                },
            )
            require(
                len(canonical(frozen)) <= MAX_BYTES,
                "FACTOR_INPUT_LIMIT",
                "Frozen selection exceeds 32 MiB",
            )
            return frozen
        except DataError as exc:
            raise DomainError(
                exc.code, exc.message, 422, details=exc.details, origin="ashare-data"
            ) from exc
        except (ValueError, KeyError, TypeError, IndexError, ArithmeticError, OSError) as exc:
            raise DomainError(
                "FACTOR_SOURCE_INPUT",
                "Registered input is unavailable or malformed: " + str(exc),
                422,
            ) from exc


def market_panel(owner, spec, request):
    """Build the existing Alpha191 input protocol from the owner's frozen raw read."""
    raw = owner.prices(
        list(request.securities),
        str(request.history_start),
        str(request.label_end or request.end_date),
        source="canonical",
    )
    sessions = raw["calendar"]
    lookup = {(r["date"], r["security"]): r for r in raw["rows"]}
    frames = {f: [] for f in ("open", "high", "low", "close", "volume", "amount", "vwap")}
    for day in sessions:
        cells = {f: [] for f in frames}
        for sid in request.securities:
            row = lookup.get((day, sid), {})
            for field in frames:
                value = row.get(field)
                if field == "vwap":
                    from decimal import Decimal

                    amount, volume = row.get("amount"), row.get("volume")
                    value = (
                        str(Decimal(str(amount)) / Decimal(str(volume)))
                        if amount is not None and volume is not None and Decimal(str(volume)) > 0
                        else None
                    )
                cells[field].append(value)
        for field in frames:
            frames[field].append(cells[field])
    return (
        dict(
            schema="alpha191.panel.v1",
            sessions=sessions,
            securities=list(request.securities),
            universe=[[True] * len(request.securities) for _ in sessions],
            fields=frames,
            adjustment="raw",
            units={"price": "CNY/share", "volume": "share", "amount": "CNY"},
        ),
        {
            "market_release": owner.release.receipt(),
            "read_receipt": raw["receipt"],
            "gaps": raw["gaps"],
            "coverage": raw["coverage"],
            "vwap": "same-row raw amount / share volume; absent or zero volume remains missing",
        },
        market_labels(raw, spec, request) if request.evaluation is not None else None,
    )
