"""Task-oriented MCP v2 server over the AQ research HTTP API."""

import argparse
import asyncio
import json
import time
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from ashare_quant.mcp.client import PREFIXES, Backend, ResearchError, Settings
from ashare_quant.mcp.contracts import (
    Cursor,
    Definition,
    EvaluationOptions,
    FactorCreate,
    Key,
    Library,
    PageSize,
    PythonCreate,
    PythonProject,
    Section,
    SourceID,
    WaitSeconds,
    Workspace,
    auto_idempotency_key,
    normalize_daily_window,
    normalize_minute_window,
    normalize_securities,
    normalize_security,
)
from ashare_quant.mcp.discovery import environment_view, require_extension
from ashare_quant.mcp.knowledge import analyze_strategy_code, register_knowledge
from ashare_quant.mcp.projection import (
    ANALYSIS_SECTIONS,
    compact,
    digest,
    page,
    result_rows,
    run_view,
)
from ashare_quant.tasks.contracts import JobID

TERMINAL_STATES = {"succeeded", "failed", "cancelled", "retired", "runtime_unavailable"}


def create_server(backend):
    # Keep the SDK optional for the API/worker and give CLI users an actionable error.
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer(
        "A宽 research",
        version="2.0.0",
        instructions=(
            "Task-oriented A-share quantitative research MCP (v2). "
            "Read resource aquant://api-reference for supported strategy APIs and aquant://templates/* for examples. "
            "Validate strategy code before running with check_strategy(). "
            "Use run_backtest() or run_factor() directly with flat parameters; they auto-generate idempotency keys, "
            "wait for completion by default (wait_seconds=30), and return 20 returns-v2 metrics or factor evaluations. "
            "Use get_run() and get_run_details() with run_id to inspect results or failure logs, and compare_runs() to compare experiments."
        ),
    )
    register_knowledge(server)

    def tool(*, read=True, idempotent=True, destructive=False, open_world=False):
        def register(fn):
            from functools import wraps

            @wraps(fn)
            async def checked(*args, **kwargs):
                try:
                    result = await fn(*args, **kwargs)
                    if len(json.dumps(result, ensure_ascii=False).encode()) > 128 * 1024:
                        raise ResearchError(
                            "CONTEXT_LIMIT",
                            "Response exceeds 128 KiB; narrow the selection or reduce limit",
                        )
                    return result
                except ResearchError as exc:
                    raise ToolError(str(exc)) from exc

            registered = server.tool(
                annotations=ToolAnnotations(
                    readOnlyHint=read,
                    destructiveHint=destructive,
                    idempotentHint=idempotent,
                    openWorldHint=open_world,
                )
            )(checked)
            tool_def = server._tool_manager._tools.get(checked.__name__)
            if tool_def is not None:
                tool_def.fn_metadata.arg_model.model_config["extra"] = "forbid"
                tool_def.fn_metadata.arg_model.model_rebuild(force=True)
                tool_def.parameters["additionalProperties"] = False
            return registered

        return register

    async def configuration():
        value = await backend.request("GET", "/api/v1/product/config")
        if not value.get("configured"):
            raise ResearchError(
                "ENVIRONMENT_NOT_CONFIGURED",
                "Product market data is not configured on the backend; call get_environment() to inspect status.",
            )
        return value

    def _workspace_headers(workspace: str | None) -> dict[str, str] | None:
        return {"X-Quant-Workspace": workspace} if workspace else None

    async def _resolve_strategy_ref(
        source: str | None,
        project: PythonProject | None,
        strategy_ref: str | None,
        workspace: str | None,
    ) -> tuple[str | None, PythonProject | None, str | None]:
        headers = _workspace_headers(workspace)
        if source is not None and project is not None:
            raise ResearchError(
                "AMBIGUOUS_STRATEGY_INPUT",
                "Supply either single-file 'source' or multi-file 'project', not both.",
            )
        if source is not None:
            return source, None, strategy_ref
        if project is not None:
            return None, project, project.project_id
        if not strategy_ref:
            raise ResearchError(
                "STRATEGY_SOURCE_REQUIRED",
                "Provide 'source', 'project', or 'strategy_ref' (saved workbench file path or aqproj-... ID).",
            )
        if strategy_ref.startswith("aqproj-"):
            loaded = await backend.request(
                "GET",
                "/api/v1/python/projects",
                params={"strategy_id": strategy_ref},
                headers=headers,
            )
            proj = PythonProject(
                project_id=loaded["strategy_id"],
                entrypoint=loaded["entrypoint"],
                files=loaded["files"],
            )
            return None, proj, loaded["strategy_id"]
        if strategy_ref.endswith(".py"):
            loaded = await backend.request(
                "GET",
                "/api/v1/python/files",
                params={"path": strategy_ref},
                headers=headers,
            )
            return loaded["source"], None, loaded["strategy_id"]
        loaded = await backend.request(
            "GET",
            "/api/v1/python/projects",
            params={"path": strategy_ref},
            headers=headers,
        )
        proj = PythonProject(
            project_id=loaded["strategy_id"],
            entrypoint=loaded["entrypoint"],
            files=loaded["files"],
        )
        return None, proj, loaded["strategy_id"]

    async def _find_by_key(key: str, expected_body: dict | None = None, families=("python_minute", "factors")):
        seen_prefixes: set[str] = set()
        matched: list[tuple[str, dict]] = []
        for fam in families:
            prefix = PREFIXES[fam]
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            resp = await backend.optional(prefix + "/jobs")
            if not resp["available"]:
                continue
            for item in resp["value"].get("items", []):
                if item.get("request", {}).get("idempotency_key") == key:
                    resolved_fam = (
                        ("python_daily" if item.get("request", {}).get("frequency") == "1d" else "python_minute")
                        if prefix == "/api/v1/python"
                        else "factors"
                    )
                    matched.append((resolved_fam, item))
        if len(matched) > 1:
            raise ResearchError("BACKEND_IDEMPOTENCY_CONFLICT", "Multiple jobs use this idempotency_key")
        if matched and expected_body is not None and matched[0][1]["request"] != expected_body:
            raise ResearchError(
                "IDEMPOTENCY_CONFLICT",
                "This idempotency_key belongs to a different frozen request; omit idempotency_key or pass a new key for changed parameters.",
            )
        return matched[0] if matched else (None, None)

    async def _fetch_job(run_id: str) -> tuple[str, dict]:
        hist = await backend.optional("/api/v1/history/jobs/" + run_id)
        if hist["available"]:
            raw = hist["value"]
            fam = raw.get("family", "python")
            if fam == "python":
                py = await backend.optional("/api/v1/python/jobs/" + run_id)
                if py["available"]:
                    raw = py["value"]
                freq = (raw.get("request") or {}).get("frequency", "1m")
                return ("python_daily" if freq == "1d" else "python_minute"), raw
            if fam == "factors":
                fac = await backend.optional("/api/v1/factors/jobs/" + run_id)
                if fac["available"]:
                    raw = fac["value"]
                return "factors", raw
            return fam, raw
        for fam, prefix in (("python_minute", "/api/v1/python"), ("factors", "/api/v1/factors")):
            probe = await backend.optional(prefix + "/jobs/" + run_id)
            if probe["available"]:
                raw = probe["value"]
                if fam == "python_minute" and (raw.get("request") or {}).get("frequency") == "1d":
                    return "python_daily", raw
                return fam, raw
        raise ResearchError(
            "RUN_NOT_FOUND",
            f"Run '{run_id}' was not found",
            hint="Call list_runs() to inspect recent runs or pass idempotency_key to get_run().",
        )

    async def _enrich_run(family: str, job: dict, *, benchmark: str = "frozen") -> dict[str, Any]:
        state = job.get("display_status") or job.get("status")
        result = None
        analysis = None
        if state == "succeeded" and job.get("result"):
            run_id = job["job_id"]
            snap = (job.get("result") or {}).get("snapshot_sha256")
            prefix = PREFIXES.get(family, "/api/v1/history")
            res_resp = await backend.cached_get(
                ("result", run_id, snap) if snap else (),
                prefix + "/jobs/" + run_id + "/result",
            )
            result = res_resp
            if family in {"python_minute", "python_daily"}:
                ana_path = prefix + "/jobs/" + run_id + "/result/analysis"
                try:
                    analysis = await backend.cached_get(
                        ("analysis", run_id, snap, benchmark) if snap else (),
                        ana_path,
                        params={"analysis_version": "returns-v2", "benchmark": benchmark},
                    )
                except ResearchError:
                    analysis = None
        return run_view(job, family=family, result=result, analysis=analysis)

    async def _wait_for_run(
        run_id: str, wait_seconds: float, *, benchmark: str = "frozen"
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(wait_seconds)
        delay = 0.05
        family, job = await _fetch_job(run_id)
        while (job.get("display_status") or job.get("status")) not in TERMINAL_STATES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 1.6, 0.5)
            family, job = await _fetch_job(run_id)
        return await _enrich_run(family, job, benchmark=benchmark)

    async def _submit(body: dict, family: str, *, headers=None, wait_seconds: float = 0.0, benchmark: str = "frozen"):
        prefix = PREFIXES[family]
        try:
            created = await backend.request("POST", prefix + "/jobs", body=body, headers=headers)
        except ResearchError as exc:
            if (
                exc.payload["code"]
                not in {"BACKEND_UNREACHABLE", "BACKEND_PROTOCOL", "BACKEND_RESPONSE_LIMIT"}
                and exc.payload.get("status", 0) < 500
            ):
                raise
            try:
                found_fam, original = await _find_by_key(
                    body["idempotency_key"], body, families=(family,)
                )
            except ResearchError as lookup:
                if lookup.payload["code"] == "IDEMPOTENCY_CONFLICT":
                    raise
                found_fam, original = None, None
            if original:
                view = await _enrich_run(found_fam or family, original, benchmark=benchmark)
                return dict(**view, recovered_after_timeout=True)
            raise ResearchError(
                "SUBMISSION_UNCERTAIN",
                "No definitive submission response. Recover by calling get_run(idempotency_key=...) or retry the identical call with the same idempotency_key.",
                idempotency_key=body["idempotency_key"],
                outcome="uncertain",
            ) from exc
        if wait_seconds > 0:
            return await _wait_for_run(created["job_id"], wait_seconds, benchmark=benchmark)
        return await _enrich_run(family, created, benchmark=benchmark)

    @tool()
    async def get_environment(detail: bool = False) -> dict[str, Any]:
        """Get concise environment readiness, available calendar range, frequencies, factor sources, and limits."""
        return await environment_view(backend, detail=detail)

    @tool()
    async def search_securities(
        query: Annotated[str, Field(max_length=100)] = "",
        limit: PageSize = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Search published A-share securities by code or name (auto-normalizes .SZ/.SH/sz/sh/6-digit codes)."""
        await configuration()
        q = query.strip()
        if q:
            try:
                q = normalize_security(q)
            except ResearchError:
                pass
        catalogue = await backend.request(
            "GET", "/api/v1/product/catalogue", params=dict(query=q, limit=limit, offset=offset)
        )
        sources = await backend.optional("/api/v1/product/minute/sources")
        ms_val = sources.get("value") or {}
        return dict(
            query=query,
            total=catalogue.get("total", 0),
            offset=catalogue.get("offset", offset),
            limit=catalogue.get("limit", limit),
            securities=catalogue.get("securities", []),
            minute_sources={
                "default_source": ms_val.get("default_source"),
                "sources": [
                    s.get("source") if isinstance(s, dict) else s
                    for s in (ms_val.get("sources") or [])
                ],
            }
            if sources["available"]
            else None,
        )

    @tool()
    async def get_prices(
        securities: list[str] | str,
        start: str,
        end: str,
        frequency: Literal["1d", "1m"] = "1d",
        fields: list[str] | None = None,
        source: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")] = "auto",
        reference_set: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Read bounded daily ('1d', <=8 securities, <=31 days) or minute ('1m', 1 security, <=3 days) prices.

        Auto-normalizes .SZ/.SH codes and completes +08:00 timezone on minute timestamps.
        """
        cfg = await configuration()
        cid = cfg.get("configuration_id") or "default"
        codes = normalize_securities(securities)

        def _filter_fields(rows: list[dict]) -> list[dict]:
            if not fields:
                return rows
            keep = {"security", "date", "at", "source"} | set(fields)
            return [{k: v for k, v in r.items() if k in keep} for r in rows]

        if frequency == "1m":
            if len(codes) != 1:
                raise ResearchError(
                    "MINUTE_SINGLE_SECURITY",
                    f"Minute price queries require exactly 1 security (got {len(codes)})",
                    hint="Pass a single security for frequency='1m', or use frequency='1d' for multiple securities.",
                )
            s_iso, e_iso = normalize_minute_window(start, end, max_days=3)
            body = dict(security=codes[0], start=s_iso, end=e_iso, source=source)
            result = await backend.request("POST", "/api/v1/product/minute/prices", body=body)
            rows = _filter_fields(result["rows"])
            return dict(
                frequency="1m",
                metadata=compact({k: v for k, v in result.items() if k != "rows"}),
                page=page(rows, ["1m", cid, body, fields], limit, cursor),
            )

        if len(codes) > 8:
            raise ResearchError(
                "TOO_MANY_SECURITIES",
                f"Daily price inspection allows at most 8 securities per call (got {len(codes)})",
                hint="Query <= 8 securities per call or use run_backtest / run_factor for universe-scale analysis.",
            )
        s_iso, e_iso = normalize_daily_window(start, end, max_days=31)
        cov_body: dict[str, Any] = dict(securities=codes, start=s_iso, end=e_iso, source=source)
        if reference_set is not None:
            cov_body["reference_set"] = reference_set
        try:
            coverage = await backend.request("POST", "/api/v1/product/coverage", body=cov_body)
        except ResearchError as exc:
            coverage = {"available": False, "ready": False, "error": exc.payload}
        price_body = dict(securities=codes, start=s_iso, end=e_iso, source=source)
        prices = await backend.request("POST", "/api/v1/product/prices", body=price_body)
        rows = _filter_fields(prices["rows"])
        return dict(
            frequency="1d",
            coverage=compact(coverage),
            metadata=compact({k: v for k, v in prices.items() if k != "rows"}),
            page=page(rows, ["1d", cid, cov_body, fields], limit, cursor),
        )

    @tool()
    async def discover_factors(
        query: Annotated[str, Field(max_length=100)] = "",
        library: Library | None = None,
        source_id: SourceID | None = None,
        version: Definition = "gtja191.explicit.v1",
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Search the 368 registered factors (joinquant, alpha191, qlib_alpha158) and list factor sources.

        Pass source_id to page the securities registered in a specific factor data source.
        """
        await require_extension(backend, "factors")
        sources_val = await backend.request("GET", "/api/v1/factors/sources")
        env = sources_val.get("environment_id") or "default"
        if source_id is not None:
            row = next((r for r in sources_val["items"] if r["id"] == source_id), None)
            if row is None:
                raise ResearchError(
                    "FACTOR_SOURCE_NOT_FOUND",
                    f"Factor source '{source_id}' is not registered",
                    hint=f"Available sources: {[r['id'] for r in sources_val['items']]}",
                )
            return dict(
                source=compact({k: v for k, v in row.items() if k != "securities"}),
                page=page(
                    [{"security": s} for s in row["securities"]],
                    [env, source_id],
                    limit,
                    cursor,
                ),
            )
        value = await backend.request(
            "GET", "/api/v1/factors/catalogue", params={"version": version}
        )
        rows = [
            compact(r)
            for r in value["items"]
            if (library is None or r["library"] == library)
            and query.casefold() in (r["id"] + " " + r.get("name", "") + " " + r.get("description", "")).casefold()
        ]
        compact_sources = [
            compact({k: v for k, v in r.items() if k != "securities"})
            for r in sources_val["items"]
        ]
        return dict(
            evaluation_available=value["evaluation_available"],
            limits=value["limits"],
            sources=compact_sources,
            page=page(rows, [env, library, query, version], limit, cursor),
        )

    @tool()
    async def check_strategy(
        source: str | None = None,
        project: PythonProject | None = None,
        strategy_ref: str | None = None,
        frequency: Literal["1m", "1d"] = "1m",
        securities: list[str] | str = (),
        start_date: date | None = None,
        end_date: date | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Preflight a Python strategy (syntax, initialize callback, aquant symbols, security codes, and dates) without queuing."""
        resolved_source, resolved_project, resolved_id = await _resolve_strategy_ref(
            source, project, strategy_ref, workspace
        )
        if resolved_project is not None:
            files = dict(resolved_project.files)
            entrypoint = resolved_project.entrypoint
        else:
            files = {"strategy.py": resolved_source or ""}
            entrypoint = "strategy.py"

        report = analyze_strategy_code(files, entrypoint=entrypoint)
        issues = list(report["issues"])
        norm_secs: list[str] = []
        if securities:
            try:
                norm_secs = normalize_securities(securities, allow_empty=True)
            except ResearchError as exc:
                issues.append(
                    dict(
                        severity="error",
                        code=exc.payload["code"],
                        file=entrypoint,
                        line=1,
                        message=exc.payload["message"],
                        fix=exc.payload.get("hint"),
                    )
                )

        if start_date and end_date and start_date > end_date:
            issues.append(
                dict(
                    severity="error",
                    code="INVALID_DATE_RANGE",
                    file=entrypoint,
                    line=1,
                    message=f"start_date ({start_date}) must be on or before end_date ({end_date})",
                    fix="Swap or adjust start_date and end_date.",
                )
            )
        elif start_date and end_date:
            py_cat = await backend.optional("/api/v1/python/catalogue")
            avail = (py_cat.get("value") or {}).get("data_availability") or {}
            cal_start = avail.get("first_execution_session") or avail.get("calendar_start")
            cal_end = avail.get("last_execution_session") or avail.get("calendar_end")
            if cal_start and str(start_date) < cal_start:
                issues.append(
                    dict(
                        severity="warning",
                        code="START_BEFORE_CALENDAR",
                        file=entrypoint,
                        line=1,
                        message=f"start_date {start_date} is earlier than first execution session {cal_start}",
                        fix=f"Use start_date >= '{cal_start}'.",
                    )
                )
            if cal_end and str(end_date) > cal_end:
                issues.append(
                    dict(
                        severity="warning",
                        code="END_AFTER_CALENDAR",
                        file=entrypoint,
                        line=1,
                        message=f"end_date {end_date} is after last execution session {cal_end}",
                        fix=f"Use end_date <= '{cal_end}'.",
                    )
                )

        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]
        return dict(
            valid=len(errors) == 0,
            error_count=len(errors),
            warning_count=len(warnings),
            frequency=frequency,
            strategy_id=resolved_id,
            normalized_securities=norm_secs,
            issues=issues,
        )

    @tool(read=False, open_world=True)
    async def run_backtest(
        start_date: date,
        end_date: date,
        source: str | None = None,
        project: PythonProject | None = None,
        strategy_ref: str | None = None,
        name: Annotated[str, Field(min_length=1, max_length=80)] = "Backtest",
        frequency: Literal["1m", "1d"] = "1m",
        initial_cash: Annotated[float, Field(gt=0, le=10_000_000_000)] = 100000,
        securities: list[str] | str = (),
        parameters: dict[str, Any] = Field(default_factory=dict),
        warmup_sessions: Annotated[int, Field(ge=0)] = 20,
        idempotency_key: Key | None = None,
        workspace: Workspace | None = None,
        benchmark: str = "frozen",
        wait_seconds: WaitSeconds = 30.0,
    ) -> dict[str, Any]:
        """Submit a daily ('1d') or minute ('1m') Python strategy backtest and optionally wait for results.

        Automatically normalizes security codes, generates an idempotency key when omitted,
        and returns the unified run view with 20 returns-v2 performance metrics on completion.
        """
        family = "python_daily" if frequency == "1d" else "python_minute"
        if family == "python_daily":
            await require_extension(backend, "python_daily")
        resolved_source, resolved_project, resolved_id = await _resolve_strategy_ref(
            source, project, strategy_ref, workspace
        )
        norm_secs = normalize_securities(securities, allow_empty=True) if securities else []
        seed_payload = dict(
            name=name,
            source=resolved_source,
            project=resolved_project.model_dump(mode="json") if resolved_project else None,
            strategy_id=resolved_id,
            frequency=frequency,
            start_date=str(start_date),
            end_date=str(end_date),
            initial_cash=initial_cash,
            securities=norm_secs,
            parameters=parameters,
            warmup_sessions=warmup_sessions,
            workspace=workspace,
        )
        key = idempotency_key or auto_idempotency_key("bt", seed_payload)
        submission = PythonCreate(
            name=name,
            idempotency_key=key,
            source=resolved_source,
            project=resolved_project,
            strategy_id=resolved_id,
            parameters=parameters,
            frequency=frequency,
            warmup_sessions=warmup_sessions,
            securities=tuple(norm_secs),
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            accept_research_model=True,
            trusted_local_code=True,
        )
        body = submission.model_dump(mode="json")
        return await _submit(
            body,
            family,
            headers=_workspace_headers(workspace),
            wait_seconds=wait_seconds,
            benchmark=benchmark,
        )

    @tool(read=False)
    async def run_factor(
        factor_id: Annotated[str, Field(min_length=1, max_length=100)],
        start_date: date,
        end_date: date,
        source_id: SourceID | None = None,
        securities: list[str] | str = "all",
        history_start: date | None = None,
        label_end: date | None = None,
        definition: Definition | None = None,
        name: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
        parameters: dict[str, Any] = Field(default_factory=dict),
        rank_policy: Literal["strict", "available"] = "strict",
        evaluation: EvaluationOptions | bool | None = True,
        idempotency_key: Key | None = None,
        wait_seconds: WaitSeconds = 30.0,
    ) -> dict[str, Any]:
        """Submit a factor computation and optional Alphalens evaluation, waiting up to wait_seconds.

        Resolves short factor names (e.g. 'alpha014' or 'MA5'), default definition, default ready source_id,
        and 'all' securities automatically when omitted.
        """
        await require_extension(backend, "factors")
        sources_resp = await backend.request("GET", "/api/v1/factors/sources")
        source_items = sources_resp.get("items", [])

        if source_id is None:
            ready_sources = [s for s in source_items if s.get("ready")]
            if not ready_sources:
                raise ResearchError(
                    "NO_READY_FACTOR_SOURCE",
                    "No ready factor data source is registered on the backend",
                )
            chosen_source = ready_sources[0]
            resolved_source_id = chosen_source["id"]
        else:
            chosen_source = next((s for s in source_items if s["id"] == source_id), None)
            if chosen_source is None:
                raise ResearchError(
                    "FACTOR_SOURCE_NOT_FOUND",
                    f"Factor source '{source_id}' is not registered",
                    hint=f"Available sources: {[s['id'] for s in source_items]}",
                )
            resolved_source_id = source_id

        cat_resp = await backend.request("GET", "/api/v1/factors/catalogue")
        cat_items = cat_resp.get("items", [])
        exact = next((item for item in cat_items if item["id"] == factor_id), None)
        if exact is None:
            suffix_matches = [
                item
                for item in cat_items
                if item.get("field", "").casefold() == factor_id.casefold()
                or item["id"].casefold() == factor_id.casefold()
            ]
            if len(suffix_matches) == 1:
                exact = suffix_matches[0]
            elif len(suffix_matches) > 1:
                raise ResearchError(
                    "AMBIGUOUS_FACTOR_ID",
                    f"Multiple factors match '{factor_id}': {[m['id'] for m in suffix_matches]}",
                    hint="Pass the full namespaced factor_id, e.g. 'alpha191:alpha001' or 'qlib_alpha158:MA5'.",
                )
        resolved_factor_id = exact["id"] if exact else factor_id
        resolved_def = definition or (exact["definition"] if exact else "gtja191.explicit.v1")

        if securities == "all" or securities == ["all"]:
            norm_secs = list(chosen_source.get("securities") or [])
            if not norm_secs:
                raise ResearchError(
                    "EMPTY_FACTOR_SOURCE_SECURITIES",
                    f"Factor source '{resolved_source_id}' has no registered securities",
                )
        else:
            norm_secs = normalize_securities(securities)

        eval_opts: EvaluationOptions | None
        if evaluation is True:
            eval_opts = EvaluationOptions()
        elif evaluation is False or evaluation is None:
            eval_opts = None
        else:
            eval_opts = evaluation

        hist_start = history_start or start_date
        run_name = name or f"Factor {resolved_factor_id}"
        seed_payload = dict(
            name=run_name,
            factor_id=resolved_factor_id,
            definition=resolved_def,
            source_id=resolved_source_id,
            securities=norm_secs,
            history_start=str(hist_start),
            start_date=str(start_date),
            end_date=str(end_date),
            label_end=str(label_end) if label_end else None,
            parameters=parameters,
            rank_policy=rank_policy,
            evaluation=eval_opts.model_dump(mode="json") if eval_opts else None,
        )
        key = idempotency_key or auto_idempotency_key("fac", seed_payload)
        submission = FactorCreate(
            idempotency_key=key,
            name=run_name,
            factor_id=resolved_factor_id,
            definition=resolved_def,
            source_id=resolved_source_id,
            securities=tuple(norm_secs),
            history_start=hist_start,
            start_date=start_date,
            end_date=end_date,
            label_end=label_end,
            parameters=parameters,
            rank_policy=rank_policy,
            evaluation=eval_opts,
        )
        body = submission.model_dump(mode="json")
        return await _submit(body, "factors", wait_seconds=wait_seconds)

    @tool()
    async def get_run(
        run_id: JobID | None = None,
        idempotency_key: Key | None = None,
        benchmark: str = "frozen",
        wait_seconds: WaitSeconds = 0.0,
    ) -> dict[str, Any]:
        """Get unified run state, progress, 20 returns-v2 metrics (or factor summary), or failure diagnostics + strategy logs.

        Look up by run_id or idempotency_key without needing a family parameter.
        """
        if not run_id and not idempotency_key:
            raise ResearchError(
                "RUN_IDENTIFIER_REQUIRED",
                "Provide either run_id or idempotency_key to get_run()",
            )
        if not run_id:
            fam, job = await _find_by_key(idempotency_key)
            if not job:
                return dict(
                    found=False,
                    job=None,
                    note="Not found is not proof of non-execution during an in-flight request",
                )
            run_id = job["job_id"]
        if wait_seconds > 0:
            return await _wait_for_run(run_id, wait_seconds, benchmark=benchmark)
        fam, job = await _fetch_job(run_id)
        return await _enrich_run(fam, job, benchmark=benchmark)

    @tool()
    async def get_run_details(
        run_id: JobID,
        section: Section = "trades",
        benchmark: str = "frozen",
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Page detailed run records (trades, positions, equity, orders, strategy_logs, analysis_series, factor_values, ic, system_logs).

        Works without a family parameter, and supports strategy_logs even when a Python strategy run failed.
        """
        family, job = await _fetch_job(run_id)
        prefix = PREFIXES.get(family, "/api/v1/history")
        if section == "system_logs":
            logs = await backend.request("GET", prefix + "/jobs/" + run_id + "/logs")
            rows = result_rows(None, family, "system_logs", system_logs=logs["items"])
            return page(rows, [family, run_id, "system_logs"], limit, cursor)

        state = job.get("display_status") or job.get("status")
        if state != "succeeded" or not job.get("result"):
            rows = result_rows(None, family, section, error=job.get("error"))
            return page(rows, [family, run_id, section, "failed"], limit, cursor)

        snap = (job.get("result") or {}).get("snapshot_sha256")
        result = await backend.cached_get(
            ("result", run_id, snap) if snap else (),
            prefix + "/jobs/" + run_id + "/result",
        )
        analysis = None
        if section in ANALYSIS_SECTIONS and family in {"python_minute", "python_daily"}:
            ana_path = prefix + "/jobs/" + run_id + "/result/analysis"
            analysis = await backend.cached_get(
                ("analysis", run_id, snap, benchmark) if snap else (),
                ana_path,
                params={"analysis_version": "returns-v2", "benchmark": benchmark},
            )
        rows = result_rows(result, family, section, analysis=analysis, error=job.get("error"))
        identity = [family, run_id, section, digest(result)]
        return page(rows, identity, limit, cursor)

    @tool()
    async def list_runs(
        kind: Literal["all", "python", "factors"] = "all",
        strategy_id: str | None = None,
        state: str | None = None,
        limit: PageSize = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """List recent backtest and factor runs with their status, parameters, and headline metrics."""
        if strategy_id is not None:
            resp = await backend.request(
                "GET",
                "/api/v1/python/backtests",
                params={
                    "strategy_id": strategy_id,
                    "limit": min(limit, 100),
                    "offset": offset,
                    "analysis_version": "returns-v2",
                },
            )
            items = [compact(item) for item in resp.get("items", [])]
            if state:
                items = [i for i in items if (i.get("display_status") or i.get("status")) == state]
            return dict(
                strategy_id=strategy_id,
                total=resp.get("total", len(items)),
                offset=offset,
                limit=limit,
                items=items,
            )

        params = {"family": kind} if kind in {"python", "factors"} else None
        resp = await backend.request("GET", "/api/v1/history/jobs", params=params)
        raw_items = list(resp.get("items", []))
        raw_items.sort(key=lambda r: (r.get("created_at") or "", r.get("job_id") or ""), reverse=True)
        if state:
            raw_items = [
                r for r in raw_items if (r.get("display_status") or r.get("status")) == state
            ]
        sliced = raw_items[offset : offset + limit]
        items = [run_view(r) for r in sliced]
        return dict(total=len(raw_items), offset=offset, limit=limit, items=items)

    @tool()
    async def compare_runs(
        run_ids: Annotated[list[JobID], Field(min_length=2, max_length=10)],
        benchmark: str = "frozen",
    ) -> dict[str, Any]:
        """Compare 2-10 runs side-by-side across parameters, returns-v2 performance metrics, or factor IC summaries."""
        runs = []
        for rid in run_ids:
            fam, job = await _fetch_job(rid)
            runs.append(await _enrich_run(fam, job, benchmark=benchmark))

        param_sets = [r.get("submission", {}).get("parameters") or {} for r in runs]
        all_param_keys = sorted({k for p in param_sets for k in p})
        varying_params = {
            k: [p.get(k) for p in param_sets]
            for k in all_param_keys
            if len({json.dumps(p.get(k), sort_keys=True, default=str) for p in param_sets}) > 1
        }

        comparison_rows = []
        for r in runs:
            sub = r.get("submission") or {}
            row = dict(
                job_id=r["job_id"],
                name=sub.get("name"),
                family=r["family"],
                state=r["state"],
                frequency=sub.get("frequency"),
                start_date=sub.get("start_date"),
                end_date=sub.get("end_date"),
                parameters=sub.get("parameters") or {},
                summary=r.get("summary"),
                metrics=r.get("metrics"),
                evaluation_summary=r.get("evaluation_summary"),
            )
            comparison_rows.append({k: v for k, v in row.items() if v is not None})

        return dict(
            count=len(runs),
            benchmark=benchmark,
            varying_parameters=varying_params,
            runs=comparison_rows,
        )

    @tool(read=False, destructive=True)
    async def cancel_run(run_id: JobID) -> dict[str, Any]:
        """Request cancellation of a queued or running Python backtest or factor job by run_id."""
        family, _ = await _fetch_job(run_id)
        if family in {"portfolio", "daily"}:
            raise ResearchError("TASK_FAMILY_RETIRED", "Historical tasks are read-only")
        prefix = PREFIXES[family]
        cancelled = await backend.request("POST", prefix + "/jobs/" + run_id + "/cancel", body={})
        return run_view(cancelled, family=family)

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True, help="Explicit AQ loopback origin")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8766, help="Loopback MCP HTTP port")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    try:
        settings = Settings(args.backend_url, args.timeout)
        server = create_server(Backend(settings))
    except (ValueError, ImportError) as exc:
        parser.error(f"{exc}; install ashare-quant[mcp] and supply a loopback backend")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be 1..65535")
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=args.port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
