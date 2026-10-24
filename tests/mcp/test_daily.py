"""Synthetic daily Python acceptance is separate from real-market validation."""

import asyncio

from mcp import Client

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import result_rows
from ashare_quant.mcp.server import create_server
from ashare_quant.tasks.worker import work_one
from tests.mcp.fixture import make_deployment, submission
from tests.mcp.test_e2e import call, collect, serving

SOURCE = """from aquant import *
def initialize(context):
    set_universe(context.securities)
    run_daily(trade, time="open")
def trade(context):
    previous = attribute_history(context.securities[0], 1, "1d", ["close"], fq=None)
    log.info("synthetic daily previous close", float(previous.close.iloc[-1]))
    order_target(context.securities[0], context.parameters["shares"])
"""


def test_daily_request_consent_native_results_and_held_positions(tmp_path):
    app = create_app(product_configuration=make_deployment(tmp_path / "lab"))
    with serving(app) as url:
        backend = Backend(Settings(url))

        async def scenario():
            async with Client(create_server(backend)) as c:
                caps = await call(c, "research_capabilities")
                daily = caps["extensions"]["python_daily"]
                assert daily["available"]
                assert daily["limits"]["terminal_positions"] == "marked_at_final_close"
                body = submission(daily["environment_id"], source=SOURCE) | {
                    "frequency": "1d",
                    "warmup_sessions": 1,
                }
                for field in ("trusted_local_code", "accept_research_model"):
                    invalid = {k: v for k, v in body.items() if k != field}
                    assert (await c.call_tool("submit_strategy", {"submission": invalid})).is_error
                assert (await backend.request("GET", "/api/v1/python/jobs"))["items"] == []
                job = await call(c, "submit_strategy", submission=body)
                assert job["submission"]["frequency"] == "1d"
                assert (await call(c, "submit_strategy", submission=body))["job_id"] == job[
                    "job_id"
                ]
                await asyncio.to_thread(work_one, app.state.tasks, job["job_id"])
                state = await call(c, "research_job", family="python_daily", job_id=job["job_id"])
                assert state["status"] == "succeeded", state
                direct = await backend.request(
                    "GET", "/api/v1/python/jobs/" + job["job_id"] + "/result"
                )
                summary = await call(
                    c, "research_result", family="python_daily", job_id=job["job_id"]
                )
                assert summary["summary"] == direct["summary"]
                assert direct["native"]["accounts"][-1]["positions"]
                for section in summary["sections"]:
                    assert await collect(
                        c,
                        "research_result",
                        family="python_daily",
                        job_id=job["job_id"],
                        section=section,
                    ) == result_rows(direct, "python_daily", section)

        asyncio.run(scenario())
