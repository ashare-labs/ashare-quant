"""A thin MCP facade over the existing AQ research HTTP API."""

import argparse
import json
from typing import Annotated, Any

from pydantic import Field

from ashare_quant.mcp.client import PREFIXES, Backend, ResearchError, Settings
from ashare_quant.mcp.contracts import (
    Cursor,
    DailySelection,
    Family,
    Key,
    MinuteSelection,
    PageSize,
    Section,
    Sha,
    StrategySubmission,
)
from ashare_quant.mcp.projection import compact, digest, job_view, page, result_rows
from ashare_quant.tasks.daily.contracts import JobID


def create_server(backend):
    # Keep the SDK optional for the API/worker and give CLI users an actionable error.
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer(
        "A宽 research",
        version="1.0.0",
        instructions=(
            "Discover capabilities first. Use only published research data. Keep configuration_id "
            "and environment_id from discovery. Python executes trusted local code, not a hostile-code "
            "sandbox. Obtain user confirmation of code trust and the research model; never infer it. "
            "Reuse the exact submission and idempotency key after a timeout, or find the original job. "
            "Changed parameters require a new key. Submission returns before worker execution. "
            "Read result summaries first, then bounded pages. No trading, source mutation or publishing."
        ),
    )

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
                            "Response exceeds 128 KiB; narrow the selection or use backend export",
                        )
                    return result
                except ResearchError as exc:
                    raise ToolError(str(exc)) from exc

            return server.tool(
                annotations=ToolAnnotations(
                    readOnlyHint=read,
                    destructiveHint=destructive,
                    idempotentHint=idempotent,
                    openWorldHint=open_world,
                )
            )(checked)

        return register

    async def configuration(expected):
        value = await backend.request("GET", "/api/v1/product/config")
        if not value.get("configured") or value.get("configuration_id") != expected:
            raise ResearchError(
                "ENVIRONMENT_CHANGED",
                "Product configuration unavailable or changed; rediscover and confirm the environment",
            )
        return value

    @tool()
    async def research_capabilities() -> dict[str, Any]:
        """Discover real backend availability, environment identities and research limits."""
        config = await backend.optional("/api/v1/product/config")
        minute = await backend.optional("/api/v1/python/catalogue")
        schema = await backend.optional("/api/v1/openapi.json")
        paths = schema.get("value", {}).get("paths", {})
        factors = ["/api/v1/factors/" + part for part in ("catalogue", "sources", "jobs")]
        factor_paths = {p: p in paths for p in factors}
        # The Air contracts are deliberately not invented from a path name or HTTP 200.
        return compact(
            dict(
                adapter_version="research-mcp.v1",
                backend_openapi_sha256=digest(schema["value"]) if schema["available"] else None,
                product=config,
                python_minute=minute,
                task_families={
                    family: {"available": prefix + "/jobs" in paths}
                    for family, prefix in PREFIXES.items()
                },
                extensions={
                    "factors": dict(
                        discovered_paths=factor_paths,
                        available=False,
                        reason="Air factor request/result contract and real integration pending; "
                        "no factor submission adapter enabled",
                    ),
                    "python_daily": dict(
                        available=False,
                        reason="Air custom daily Python contract pending; existing fixed daily "
                        "evaluation jobs are a separate family",
                    ),
                },
                adapter_limits=dict(
                    page_size=200,
                    daily_securities=8,
                    daily_calendar_days=31,
                    minute_calendar_days=3,
                    backend_response_bytes=32 * 1024 * 1024,
                ),
                trust="Trusted local Python only; process supervision is not a hostile-code sandbox",
            )
        )

    @tool()
    async def discover_data(
        configuration_id: Sha,
        query: Annotated[str, Field(max_length=100)] = "",
        limit: PageSize = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Search the configured published instrument catalogue and list minute data sources."""
        await configuration(configuration_id)
        catalogue = await backend.request(
            "GET", "/api/v1/product/catalogue", params=dict(query=query, limit=limit, offset=offset)
        )
        sources = await backend.optional("/api/v1/product/minute/sources")
        # Catalogue itself is already paginated by the owner; don't truncate the instrument page.
        return dict(catalogue=catalogue, minute_sources=compact(sources))

    @tool()
    async def inspect_data(
        configuration_id: Sha,
        selection: DailySelection,
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Check coverage and fetch a bounded daily price page; gaps never become invented prices."""
        await configuration(configuration_id)
        body = selection.model_dump(mode="json")
        try:
            coverage = await backend.request("POST", "/api/v1/product/coverage", body=body)
        except ResearchError as exc:
            # Reading prices grants no execution permission; preserve the coverage failure.
            coverage = {"available": False, "ready": False, "error": exc.payload}
        body.pop("reference_set", None)
        prices = await backend.request("POST", "/api/v1/product/prices", body=body)
        return dict(
            coverage=compact(coverage),
            metadata=compact({k: v for k, v in prices.items() if k != "rows"}),
            page=page(prices["rows"], [configuration_id, body], limit, cursor),
        )

    @tool()
    async def read_minute_prices(
        configuration_id: Sha,
        selection: MinuteSelection,
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Read published minute observations in pages; at most one security and three days."""
        await configuration(configuration_id)
        body = selection.model_dump(mode="json")
        result = await backend.request("POST", "/api/v1/product/minute/prices", body=body)
        return dict(
            metadata=compact({k: v for k, v in result.items() if k != "rows"}),
            page=page(result["rows"], [configuration_id, body], limit, cursor),
        )

    async def find(family, key, expected=None):
        items = (await backend.request("GET", PREFIXES[family] + "/jobs"))["items"]
        matched = [j for j in items if j.get("request", {}).get("idempotency_key") == key]
        if len(matched) > 1:
            raise ResearchError("BACKEND_IDEMPOTENCY_CONFLICT", "Multiple jobs use this key")
        if matched and expected is not None and matched[0]["request"] != expected:
            raise ResearchError(
                "IDEMPOTENCY_CONFLICT",
                "This key belongs to a different frozen request; use a new key for a changed experiment",
            )
        return matched[0] if matched else None

    @tool(read=False, open_world=True)
    async def submit_strategy(submission: StrategySubmission) -> dict[str, Any]:
        """Submit trusted custom minute Python and return job_id immediately, without waiting.

        User must explicitly accept trusted_local_code and accept_research_model. Both are
        required, never defaulted. Reuse this exact body/key after uncertain network outcomes.
        Limits: 8 stocks, 23 sessions, 20000 stock-minutes; no Python checkpoint resume.
        """
        body = submission.model_dump(mode="json")
        try:
            return job_view(await backend.request("POST", "/api/v1/python/jobs", body=body))
        except ResearchError as exc:
            if (
                exc.payload["code"]
                not in {"BACKEND_UNREACHABLE", "BACKEND_PROTOCOL", "BACKEND_RESPONSE_LIMIT"}
                and exc.payload.get("status", 0) < 500
            ):
                raise
            try:
                original = await find("python_minute", submission.idempotency_key, body)
            except ResearchError as lookup:
                if lookup.payload["code"] == "IDEMPOTENCY_CONFLICT":
                    raise
                original = None
            if original:
                return dict(**job_view(original), recovered_after_timeout=True)
            raise ResearchError(
                "SUBMISSION_UNCERTAIN",
                "No definitive submission response. Find the original job by this key; "
                "retry only the identical request with the same key. Never generate a new key.",
                idempotency_key=submission.idempotency_key,
                outcome="uncertain",
            ) from exc

    @tool()
    async def find_research_job(family: Family, idempotency_key: Key) -> dict[str, Any]:
        """Recover a lost submission response using its original idempotency key."""
        job = await find(family, idempotency_key)
        return dict(
            found=job is not None,
            job=job_view(job) if job else None,
            note="Not found is not proof of non-execution during an in-flight request",
        )

    @tool()
    async def research_job(family: Family, job_id: JobID) -> dict[str, Any]:
        """Read authoritative queue/worker status, cancellation state and actionable failure."""
        return job_view(await backend.request("GET", PREFIXES[family] + "/jobs/" + job_id))

    @tool(read=False, destructive=True)
    async def cancel_research_job(family: Family, job_id: JobID) -> dict[str, Any]:
        """Request backend cancellation; running jobs may remain cancelling until acknowledged."""
        return job_view(
            await backend.request("POST", PREFIXES[family] + "/jobs/" + job_id + "/cancel", body={})
        )

    @tool()
    async def research_logs(
        family: Family, job_id: JobID, limit: PageSize = 50, cursor: Cursor | None = None
    ) -> dict[str, Any]:
        """Read a bounded log page. If logs changed, restart pagination from the first page."""
        value = await backend.request("GET", PREFIXES[family] + "/jobs/" + job_id + "/logs")
        return page(value["items"], [family, job_id, "logs"], limit, cursor)

    @tool()
    async def research_result(
        family: Family,
        job_id: JobID,
        section: Section = "summary",
        limit: PageSize = 50,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """Read summary first, then complete bounded trades/positions/equity/orders pages.

        Detailed sections currently support python_minute. Native values are projected,
        never recalculated. Cursors bind the exact result and section.
        """
        result = await backend.request("GET", PREFIXES[family] + "/jobs/" + job_id + "/result")
        identity = [family, job_id, section, digest(result)]
        if section == "summary":
            if cursor:
                raise ResearchError("CURSOR_UNSUPPORTED", "Summary does not accept a cursor")
            return dict(
                job_id=job_id,
                result_sha256=digest(result),
                summary=result.get("summary"),
                parameters=compact(result.get("parameters")),
                release=compact(result.get("release")),
                sections=(
                    ["trades", "positions", "equity", "orders", "strategy_logs"]
                    if family == "python_minute"
                    else []
                ),
            )
        return page(result_rows(result, family, section), identity, limit, cursor)

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
