"""Real factor worker/evaluation through MCP v2 using explicitly synthetic observations."""

import asyncio
from pathlib import Path

from mcp import Client, StdioServerParameters

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import digest, leaves
from ashare_quant.mcp.server import create_server
from ashare_quant.storage.artifacts import atomic, sha
from ashare_quant.tasks.worker import work_one
from tests.support.factor_app import source as panel_source
from tests.support.mcp_transport import call, collect, serving

source = panel_source


def test_factor_discovery_compute_evaluate_and_frozen_recovery(tmp_path, source):
    import os
    import sys

    specs, payload = source
    payload["adjustment"] = "adjusted:synthetic-test-only"
    path = Path(specs["fixture"]["path"])
    atomic(path, payload)
    specs["fixture"]["sha256"] = sha(path.read_bytes())
    app = create_app(str(tmp_path / "control.sqlite3"), factor_sources=specs)
    with serving(app) as url:
        backend = Backend(Settings(url))

        async def scenario():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "ashare_quant.mcp.server", "--backend-url", url],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for transport, short_query, factor_id, definition in (
                (create_server(backend), "alpha014", "alpha191:alpha014", "gtja191.explicit.v1"),
                (params, "qlib_alpha158:MA5", "qlib_alpha158:MA5", "qlib.alpha158.v0.9.7"),
            ):
                async with Client(transport) as c:
                    env_info = await call(c, "get_environment")
                    factor = env_info["extensions"]["factors"]
                    assert factor["available"] and factor["evaluation_available"]
                    assert factor["limits"] == {}
                    assert not env_info["extensions"]["python_daily"]["available"]

                    listing = await call(c, "discover_factors", query=factor_id)
                    assert listing["limits"] == factor["limits"]
                    assert listing["sources"][0]["ready"]
                    assert "path" not in str(listing["sources"])
                    item = listing["page"]["items"][0]
                    assert item["id"] == factor_id and item["definition"] == definition

                    stocks = await collect(c, "discover_factors", source_id="fixture")
                    assert [r["security"] for r in stocks] == payload["securities"]

                    body = dict(
                        idempotency_key=item["field"],
                        name="Synthetic factor",
                        factor_id=short_query,  # Also tests short factor_id resolution
                        history_start=payload["sessions"][0],
                        start_date="2024-01-09",
                        end_date="2024-01-12",
                        label_end="2024-01-17",
                        evaluation=dict(horizons=[1], quantiles=2, min_assets=2, rolling_window=2),
                        wait_seconds=0,
                    )
                    for change in ({"path": "/etc/passwd"}, {"definition": "made-up"}):
                        assert (await c.call_tool("run_factor", body | change)).is_error

                    job = await call(c, "run_factor", **body)
                    job_id = job["job_id"]
                    assert (await call(c, "run_factor", **body))["job_id"] == job_id
                    assert (
                        await c.call_tool("run_factor", body | {"name": "other"})
                    ).is_error
                    assert (
                        await call(c, "get_run", idempotency_key=body["idempotency_key"])
                    )["job_id"] == job_id

                    # The accepted job executes the frozen bytes, even if the input changes.
                    original = path.read_bytes()
                    path.write_text("{}")
                    try:
                        await asyncio.to_thread(work_one, app.state.factor_jobs, job_id)
                    finally:
                        path.write_bytes(original)

                    status = await call(c, "get_run", run_id=job_id)
                    assert status["state"] == "succeeded", status
                    direct = await backend.request(
                        "GET", "/api/v1/factors/jobs/" + job_id + "/result"
                    )
                    assert status["result_sha256"] == digest(direct)
                    assert (
                        await collect(
                            c,
                            "get_run_details",
                            run_id=job_id,
                            section="factor_values",
                        )
                        == direct["rows"]
                    )
                    assert await collect(
                        c, "get_run_details", run_id=job_id, section="evaluation"
                    ) == list(leaves(direct["evaluation"]))
                    for section in status["sections"]:
                        if section in {"evaluation", "factor_values", "system_logs"}:
                            continue
                        expected = direct["evaluation"][
                            "summary" if section == "evaluation_summary" else section
                        ]
                        assert (
                            await collect(
                                c,
                                "get_run_details",
                                run_id=job_id,
                                section=section,
                            )
                            == expected
                        )
                    cancelled = await call(
                        c,
                        "run_factor",
                        **(body | {"idempotency_key": item["field"] + "-cancel"}),
                    )
                    assert (
                        await call(c, "cancel_run", run_id=cancelled["job_id"])
                    )["state"] == "cancelled"
                    assert (
                        await c.call_tool(
                            "get_run_details", {"run_id": cancelled["job_id"], "section": "factor_values"}
                        )
                    ).is_error

        asyncio.run(scenario())
