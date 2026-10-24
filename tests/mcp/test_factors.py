"""Real factor worker/evaluation through MCP using explicitly synthetic observations."""

import asyncio
from pathlib import Path

from mcp import Client, StdioServerParameters

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import digest, leaves
from ashare_quant.mcp.server import create_server
from ashare_quant.storage.artifacts import atomic, sha
from ashare_quant.tasks.worker import work_one
from tests.factors.test_exploration import source as panel_source
from tests.mcp.test_e2e import call, collect, serving

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
            for transport, factor_id, definition in (
                (create_server(backend), "alpha191:alpha014", "gtja191.explicit.v1"),
                (params, "qlib_alpha158:MA5", "qlib.alpha158.v0.9.7"),
            ):
                async with Client(transport) as c:
                    caps = await call(c, "research_capabilities")
                    factor = caps["extensions"]["factors"]
                    assert factor["available"] and factor["evaluation_available"]
                    assert factor["limits"] == dict(
                        max_security_sessions=100000, max_input_bytes=33554432
                    )
                    assert not caps["extensions"]["python_daily"]["available"]
                    env = factor["environment_id"]
                    listing = await call(c, "discover_factors", environment_id=env, query=factor_id)
                    assert listing["limits"] == factor["limits"]
                    item = listing["page"]["items"][0]
                    assert item["id"] == factor_id and item["definition"] == definition
                    sources = await call(c, "factor_sources", environment_id=env)
                    assert sources["page"]["items"][0]["ready"]
                    assert "path" not in str(sources)
                    stocks = await collect(
                        c, "factor_sources", environment_id=env, source_id="fixture"
                    )
                    assert [r["security"] for r in stocks] == payload["securities"]
                    body = dict(
                        environment_id=env,
                        idempotency_key=item["field"],
                        name="Synthetic factor",
                        factor_id=factor_id,
                        definition=definition,
                        source_id="fixture",
                        securities=payload["securities"],
                        history_start=payload["sessions"][0],
                        start_date="2024-01-09",
                        end_date="2024-01-12",
                        label_end="2024-01-17",
                        evaluation=dict(horizons=[1], quantiles=2, min_assets=2, rolling_window=2),
                    )
                    for change in ({"path": "/etc/passwd"}, {"definition": "made-up"}):
                        assert (
                            await c.call_tool("submit_factor", {"submission": body | change})
                        ).is_error
                    job = await call(c, "submit_factor", submission=body)
                    job_id = job["job_id"]
                    assert (await call(c, "submit_factor", submission=body))["job_id"] == job_id
                    assert (
                        await c.call_tool("submit_factor", {"submission": body | {"name": "other"}})
                    ).is_error
                    assert (
                        await call(
                            c,
                            "find_research_job",
                            family="factors",
                            idempotency_key=body["idempotency_key"],
                        )
                    )["job"]["job_id"] == job_id
                    # The accepted job executes the frozen bytes, even if the input changes.
                    original = path.read_bytes()
                    path.write_text("{}")
                    try:
                        await asyncio.to_thread(work_one, app.state.factor_jobs, job_id)
                    finally:
                        path.write_bytes(original)
                    status = await call(c, "research_job", family="factors", job_id=job_id)
                    assert status["status"] == "succeeded", status
                    direct = await backend.request(
                        "GET", "/api/v1/factors/jobs/" + job_id + "/result"
                    )
                    summary = await call(c, "research_result", family="factors", job_id=job_id)
                    assert summary["result_sha256"] == digest(direct)
                    assert (
                        await collect(
                            c,
                            "research_result",
                            family="factors",
                            job_id=job_id,
                            section="factor_values",
                        )
                        == direct["rows"]
                    )
                    assert await collect(
                        c, "research_result", family="factors", job_id=job_id, section="evaluation"
                    ) == list(leaves(direct["evaluation"]))
                    for section in summary["sections"]:
                        if section in {"evaluation", "factor_values"}:
                            continue
                        expected = direct["evaluation"][
                            "summary" if section == "evaluation_summary" else section
                        ]
                        assert (
                            await collect(
                                c,
                                "research_result",
                                family="factors",
                                job_id=job_id,
                                section=section,
                            )
                            == expected
                        )
                    cancelled = await call(
                        c,
                        "submit_factor",
                        submission=body | {"idempotency_key": item["field"] + "-cancel"},
                    )
                    assert (
                        await call(
                            c, "cancel_research_job", family="factors", job_id=cancelled["job_id"]
                        )
                    )["status"] == "cancelled"
                    assert (
                        await c.call_tool(
                            "research_result", {"family": "factors", "job_id": cancelled["job_id"]}
                        )
                    ).is_error

        asyncio.run(scenario())
