"""Synthetic daily Python acceptance and failed-run strategy log capture via MCP v2."""

import asyncio

from mcp import Client

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import result_rows
from ashare_quant.mcp.server import create_server
from ashare_quant.tasks.worker import work_one
from tests.mcp.fixture import make_deployment, submission
from tests.support.mcp_transport import call, collect, serving

SOURCE = """from aquant import *
def initialize(context):
    set_universe(context.securities)
    run_daily(trade, time="open")
def trade(context):
    previous = attribute_history(context.securities[0], 1, "1d", ["close"], fq=None)
    log.info("synthetic daily previous close", float(previous.close.iloc[-1]))
    order_target(context.securities[0], context.parameters["shares"])
"""

FAILING_SOURCE = """from aquant import *
def initialize(context):
    set_universe(context.securities)
    run_daily(trade, time="open")
def trade(context):
    log.info("before crash log entry", 42)
    raise RuntimeError("intentional daily failure")
"""


def test_daily_backtest_wait_metrics_and_failed_strategy_logs(tmp_path):
    app = create_app(product_configuration=make_deployment(tmp_path / "lab"))
    with serving(app) as url:
        backend = Backend(Settings(url))

        async def scenario():
            async with Client(create_server(backend)) as c:
                env_info = await call(c, "get_environment")
                daily = env_info["extensions"]["python_daily"]
                assert daily["available"]
                assert daily["limits"]["terminal_positions"] == "marked_at_final_close"

                check = await call(
                    c, "check_strategy", source=SOURCE, frequency="1d", securities=["000001.SZ"]
                )
                assert check["valid"] and check["normalized_securities"] == ["000001.XSHE"]

                body = submission(source=SOURCE) | {
                    "frequency": "1d",
                    "warmup_sessions": 1,
                    "securities": ["000001.SZ"],
                }
                body.pop("idempotency_key")  # Test deterministic auto-idempotency key

                job = await call(c, "run_backtest", **body)
                assert job["submission"]["frequency"] == "1d"
                assert job["submission"]["securities"] == ["000001.XSHE"]
                assert (await call(c, "run_backtest", **body))["job_id"] == job["job_id"]

                await asyncio.to_thread(work_one, app.state.tasks, job["job_id"])
                state = await call(c, "get_run", run_id=job["job_id"], wait_seconds=10)
                assert state["state"] == "succeeded", state
                assert "metrics" in state and "sharpe" in state["metrics"]

                direct = await backend.request(
                    "GET", "/api/v1/python/jobs/" + job["job_id"] + "/result"
                )
                assert state["summary"] == direct["summary"]
                assert direct["native"]["accounts"][-1]["positions"]
                for section in ("trades", "positions", "equity", "orders", "strategy_logs"):
                    assert await collect(
                        c,
                        "get_run_details",
                        run_id=job["job_id"],
                        section=section,
                    ) == result_rows(direct, "python_daily", section)

                # Test failed run preserves strategy_logs and user-only traceback
                fail_body = submission(key="daily-fail", source=FAILING_SOURCE) | {
                    "frequency": "1d",
                    "warmup_sessions": 1,
                }
                failed_job = await call(c, "run_backtest", **fail_body)
                await asyncio.to_thread(work_one, app.state.tasks, failed_job["job_id"])
                failed_state = await call(c, "get_run", run_id=failed_job["job_id"])
                assert failed_state["state"] == "failed"
                failure = failed_state["failure"]
                assert failure["file"] == "strategy.py" and failure["line"] == 7
                assert any("before crash log entry 42" in str(x) for x in failure["last_strategy_logs"])
                assert not any("native.py" in frame for frame in failure["user_traceback"])
                fail_logs = await collect(
                    c, "get_run_details", run_id=failed_job["job_id"], section="strategy_logs"
                )
                assert len(fail_logs) == 1 and "before crash log entry 42" in fail_logs[0]["message"]

        asyncio.run(scenario())
