"""A saved strategy's runs across runtimes, projected from existing frozen evidence."""

import json
from datetime import date

from fastapi import Query, Request
from pydantic import AwareDatetime

from ashare_quant.contracts import Contract, DomainError
from ashare_quant.strategies.identity import StrategyID
from ashare_quant.tasks.contracts import JobID, JSONObject, ResultReference
from ashare_quant.tasks.history import FAMILIES
from ashare_quant.tasks.minute.contracts import TaskProgress


class StrategyBacktest(Contract):
    job_id: JobID
    strategy_id: StrategyID | None
    name: str
    start_date: date
    end_date: date
    frequency: str
    initial_cash: float
    created_at: AwareDatetime
    updated_at: AwareDatetime
    progress: TaskProgress | None = None
    status: str
    stored_status: str
    display_status: str
    execution_state: str
    execution_reason: JSONObject | None
    can_cancel: bool
    error: JSONObject | None
    result: ResultReference | None
    result_available: bool
    result_error: JSONObject | None
    analysis_error: JSONObject | None
    summary: JSONObject | None


class StrategyBacktests(Contract):
    strategy_id: StrategyID | None
    identity_model: str = "saved_relative_path"
    total: int
    unassigned_count: int
    offset: int
    limit: int
    items: list[StrategyBacktest]


def listing(history, strategy_id, limit, offset):
    selected, unassigned = [], 0
    for row in history.repo.rows():
        manifest = json.loads(row["manifest"])
        if FAMILIES.get(manifest.get("schema_version")) != "python":
            continue
        identity = manifest["request"].get("strategy_id")
        unassigned += identity is None
        if identity == strategy_id:
            selected.append(row)
    selected.sort(key=lambda r: (r["created_at"], r["job_id"]), reverse=True)
    items = []
    for row in selected[offset:offset + limit]:
        view = history.view(row)  # Verifies the original immutable task identity.
        request = view["request"]
        summary, result_error, analysis_error, available = None, None, None, False
        if view["result"] is not None and view["stored_status"] == "succeeded":
            try:
                result = history.result_projection(row["job_id"])
                summary = result.get("summary")
                available = True
            except DomainError as exc:
                result_error = {"code": exc.code, "message": exc.message}
            if available:
                from ashare_quant.tasks.result_analysis import analyze

                try:
                    analysis = analyze(result, view["result"]["snapshot_sha256"])
                    # A read-time projection, never a replacement of published.json.
                    # Keep every unavailable metric null, including absent benchmarks.
                    summary = {**(summary or {}),
                               **{key: value["value"] for key, value in analysis["metrics"].items()}}
                except DomainError as exc:
                    analysis_error = exc.body()
        items.append({
            **{k: view[k] for k in ("job_id", "created_at", "updated_at", "status",
                                   "stored_status", "display_status", "execution_state",
                                   "execution_reason", "error", "result")},
            **{k: request[k] for k in ("name", "start_date", "end_date", "initial_cash")},
            "strategy_id": request.get("strategy_id"),
            "frequency": request.get("frequency", "1m"),
            "progress": view.get("progress"),
            "can_cancel": view["execution_state"] == "current_runtime"
                          and view["stored_status"] in {"queued", "running"},
            "result_available": available, "result_error": result_error,
            "analysis_error": analysis_error, "summary": summary,
        })
    return {"strategy_id": strategy_id,
            "identity_model": "stable_project_id" if strategy_id and strategy_id.startswith("aqproj-") else "saved_relative_path",
            "total": len(selected), "offset": offset, "limit": limit,
            "unassigned_count": unassigned, "items": items}


def mount(app):
    @app.get("/api/v1/python/backtests")
    def strategy_backtests(
        request: Request,
        strategy_id: StrategyID | None = None,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> StrategyBacktests:
        allowed = {"strategy_id", "limit", "offset"}
        if (set(request.query_params) - allowed
                or any(len(request.query_params.getlist(k)) > 1 for k in allowed)):
            raise DomainError("STRATEGY_QUERY_UNSUPPORTED", "Use only the documented fields", 422)
        # An omitted identity explicitly selects unassigned legacy/imported runs.
        return listing(app.state.history, strategy_id, limit, offset)
