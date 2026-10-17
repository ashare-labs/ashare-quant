"""D0 public ResearchView mapping. No collection, cache access, repair or engine admission."""

import math
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant import __version__
from ashare_quant.contracts import SHANGHAI, Contract, DomainError, Money, Security
from ashare_quant.data import ASHARE_COMMIT, ASHARE_TREE, ASHARE_VERSION, ASHARE_WHEEL_SHA256
from ashare_quant.identity import source_sha256
from ashare_quant.runtime import runtime_fingerprint

DatasetId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PriceField = Literal["open", "high", "low", "close", "volume", "money"]


class ResearchQuery(Contract):
    securities: tuple[Security, ...] = Field(min_length=1, max_length=2)
    start: date | None = None
    end: date | None = None
    count: int | None = Field(default=None, ge=1, le=1000)
    fields: tuple[PriceField, ...] = ("open", "high", "low", "close", "volume")
    visibility: Literal["snapshot", "assumed", "received", "verified"] = "snapshot"
    as_of: AwareDatetime | None = None
    adjustment: Literal["none", "pre", "post"] = "none"
    require_complete: bool = False
    require_fresh: bool = False
    require_final: bool = False
    require_tradable: bool = False

    @model_validator(mode="after")
    def valid_query(self):
        if len(set(self.securities)) != len(self.securities) or len(set(self.fields)) != len(self.fields) or not self.fields:
            raise ValueError("securities and fields must be nonempty and unique")
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must not be after end")
        if self.start and self.count is not None:
            raise ValueError("start and count are mutually exclusive")
        if (self.visibility == "snapshot") != (self.as_of is None):
            raise ValueError("snapshot has no historical clock; other visibility modes require an aware as_of")
        return self


class ResearchCoverageQuery(Contract):
    securities: tuple[Security, ...] = Field(min_length=1, max_length=2)
    start: date
    end: date

    @model_validator(mode="after")
    def bounds(self):
        if self.start > self.end or len(set(self.securities)) != len(self.securities):
            raise ValueError("invalid coverage query bounds or repeated securities")
        return self


class DailyResearchRow(Contract):
    security: Security
    source_date: date
    source_label_at: AwareDatetime
    # These are source labels, not certified bar intervals or execution timestamps.
    open: Money | None = None
    high: Money | None = None
    low: Money | None = None
    close: Money | None = None
    volume: int | None = Field(default=None, ge=0)
    money: Money | None = None


class ResearchOwner(Contract):
    name: Literal["ashare-data"] = "ashare-data"
    version: str = ASHARE_VERSION
    commit: str | None = ASHARE_COMMIT
    tree: str | None = ASHARE_TREE
    wheel_sha256: str = ASHARE_WHEEL_SHA256
    admission: Literal["not_reviewed"] = "not_reviewed"


class ResearchDescription(Contract):
    dataset_id: DatasetId
    data_owner: ResearchOwner = Field(default_factory=ResearchOwner)
    descriptor: dict
    quality: dict
    lineage: dict
    backend_execution_permission: Literal[False] = False
    scope: Literal["market_read_only_research"] = "market_read_only_research"


class ResearchRead(ResearchDescription):
    query: ResearchQuery
    rows: list[DailyResearchRow]
    report: dict
    backend_version: str = __version__
    backend_source_sha256: str
    read_completed_at: AwareDatetime
    source_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    time_semantics: Literal["source_labels_not_verified_bar_boundaries"] = "source_labels_not_verified_bar_boundaries"


class ResearchCoverage(ResearchDescription):
    query: ResearchCoverageQuery
    coverage: dict


def wire(value):
    """Preserve public reports; do not infer timestamps, prices or quality guarantees."""
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DomainError("DATA_CONTRACT_MISMATCH", "Nonfinite Decimal in public data", 422)
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise DomainError("DATA_CONTRACT_MISMATCH", "Undeclared naive report timestamp", 422)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value  # Non-price report measurements, unchanged.
    raise DomainError("DATA_CONTRACT_MISMATCH", f"Unsupported public report value: {type(value).__name__}", 422)


def verify_data_runtime():
    from ashare_quant.providers.runtime import verify
    runtime_fingerprint()
    verify()


class ResearchReader:
    def __init__(self, store_root: str | Path):
        self.store_root = Path(store_root).expanduser().resolve()

    def _view(self, dataset_id):
        verify_data_runtime()
        from ashare_data import Store
        # Public reopen only; interface acceptance is limited to D0 research reads, not execution.
        return Store(self.store_root).research(dataset_id)

    @staticmethod
    def _description(dataset_id, view):
        descriptor = view.descriptor()
        if descriptor.get("dataset_id") != dataset_id or descriptor.get("timezone") != "Asia/Shanghai":
            raise DomainError("DATA_CONTRACT_MISMATCH", "Unexpected public dataset identity/timezone", 422)
        return ResearchDescription(dataset_id=dataset_id, descriptor=wire(descriptor),
                                   quality=wire(view.quality()), lineage=wire(view.lineage()))

    @staticmethod
    def _call(operation):
        verify_data_runtime()
        from ashare_data import DataError
        try:
            return operation()
        except DataError as exc:
            raise DomainError(exc.code, exc.message, 422, details=wire(exc.details), origin="ashare-data") from exc
        except OSError as exc:
            raise DomainError("RESEARCH_READ_FAILED", f"Public research read failed: {type(exc).__name__}", 422) from exc

    def describe(self, dataset_id: str) -> ResearchDescription:
        return self._call(lambda: self._description(dataset_id, self._view(dataset_id)))

    def read(self, dataset_id: str, query: ResearchQuery) -> ResearchRead:
        def operation():
            view = self._view(dataset_id)
            description = self._description(dataset_id, view)
            if query.as_of is not None:
                view = view.at(query.as_of, visibility=query.visibility)
            result = view.get_price(list(query.securities), start_date=query.start, end_date=query.end,
                count=query.count, fields=list(query.fields), frequency="daily",
                fq=None if query.adjustment == "none" else query.adjustment,
                require_complete=query.require_complete, require_fresh=query.require_fresh,
                require_final=query.require_final, require_tradable=query.require_tradable)
            public = result.to_dict()
            rows = []
            for original in public["rows"]:
                label = original["time"]
                if not isinstance(label, datetime):
                    raise DomainError("DATA_CONTRACT_MISMATCH", "Expected a public daily source label", 422)
                # A数达 explicitly declares these naive labels as Asia/Shanghai. No bar_end inference.
                label = label.replace(tzinfo=SHANGHAI) if label.tzinfo is None else label.astimezone(SHANGHAI)
                row = {"security": original["code"], "source_date": label.date(), "source_label_at": label}
                for field in query.fields:
                    value = original[field]
                    if field == "volume":
                        if not isinstance(value, int) or isinstance(value, bool):
                            raise DomainError("DATA_CONTRACT_MISMATCH", "Volume must arrive as integer shares", 422)
                        row[field] = value
                    else:
                        if not isinstance(value, Decimal):
                            raise DomainError("DATA_CONTRACT_MISMATCH", "Prices/money must arrive as source Decimal, not float", 422)
                        row[field] = wire(value)
                rows.append(DailyResearchRow(**row))
            return ResearchRead(**description.model_dump(), query=query, rows=rows, report=wire(public["report"]),
                backend_source_sha256=source_sha256(), read_completed_at=datetime.now(timezone.utc))
        return self._call(operation)

    def coverage(self, dataset_id: str, query: ResearchCoverageQuery) -> ResearchCoverage:
        def operation():
            view = self._view(dataset_id)
            description = self._description(dataset_id, view)
            coverage = {sid: view.coverage(sid, query.start, query.end, frequency="daily") for sid in query.securities}
            return ResearchCoverage(**description.model_dump(), query=query, coverage=wire(coverage))
        return self._call(operation)
