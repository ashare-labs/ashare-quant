"""Real MCP v2 transports -> loopback HTTP -> immutable AD -> queue -> native RQAlpha."""

import asyncio
import json
import os
import sys

import httpx
import pytest
from mcp import Client, StdioServerParameters

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import digest
from ashare_quant.mcp.server import create_server
from ashare_quant.tasks.worker import work_one
from tests.mcp.fixture import DAYS, SID, make_deployment, submission
from tests.support.mcp_transport import call, collect, serving


def test_real_transports_backend_and_parameter_iteration(tmp_path):
    pytest.importorskip("rqalpha")
    config = make_deployment(tmp_path / "lab")
    app = create_app(product_configuration=config)
    receipt = dict(data="synthetic engineering observations; not real-market acceptance", jobs=[])
    with serving(app) as url:
        backend = Backend(Settings(url))
        mcp = create_server(backend)
        with serving(mcp.streamable_http_app(stateless_http=True, json_response=True)) as mcp_url:

            async def scenario():
                async with httpx.AsyncClient(trust_env=False) as http:
                    refused = await http.post(
                        mcp_url + "/mcp", json={}, headers={"Origin": "https://untrusted.example"}
                    )
                    assert refused.status_code == 403
                    refused = await http.post(
                        mcp_url + "/mcp", json={}, headers={"Host": "untrusted.example"}
                    )
                    assert refused.status_code == 421
                for mode in ("auto", "legacy"):
                    async with Client(mcp_url + "/mcp", mode=mode) as client:
                        assert len((await client.list_tools()).tools) == 12
                        env_info = await call(client, "get_environment", detail=True)
                        assert env_info["ready"]
                        assert all(f["available"] for f in env_info["task_families"].values())
                        receipt.setdefault("http_protocols", []).append(client.protocol_version)
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-B", "-m", "ashare_quant.mcp.server", "--backend-url", url],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                async with Client(params) as client:
                    env_info = await call(client, "get_environment")
                    assert env_info["ready"]
                    assert env_info["extensions"]["factors"]["available"]
                    assert env_info["extensions"]["python_daily"]["available"]

                    # Search with shorthand .SZ code normalizes automatically
                    found = await call(client, "search_securities", query="000001.SZ")
                    assert found["securities"][0]["security"] == SID

                    # Daily prices without configuration_id
                    selection = dict(securities=[SID], start=DAYS[1], end=DAYS[2])
                    inspected = await call(
                        client, "get_prices", securities="000001.SZ", start=DAYS[1], end=DAYS[2]
                    )
                    assert inspected["coverage"]["ready"] is False  # no certified limits in fixture
                    direct = await backend.request("POST", "/api/v1/product/prices", body=selection)
                    assert (
                        await collect(
                            client,
                            "get_prices",
                            securities=[SID],
                            start=DAYS[1],
                            end=DAYS[2],
                        )
                        == direct["rows"]
                    )

                    # Minute prices auto-complete +08:00 timezone
                    direct_min = await backend.request(
                        "POST",
                        "/api/v1/product/minute/prices",
                        body=dict(
                            security=SID,
                            start=DAYS[1] + "T09:31:00+08:00",
                            end=DAYS[1] + "T09:35:00+08:00",
                        ),
                    )
                    assert (
                        await collect(
                            client,
                            "get_prices",
                            frequency="1m",
                            securities=SID,
                            start=DAYS[1] + "T09:31:00",
                            end=DAYS[1] + "T09:35:00",
                        )
                        == direct_min["rows"]
                    )

                    run_ids = []
                    for size in (100, 200):
                        req = submission(f"iteration-{size}", size)
                        job = await call(client, "run_backtest", **req)
                        assert (await call(client, "run_backtest", **req))["job_id"] == job[
                            "job_id"
                        ]
                        recovered = await call(
                            client,
                            "get_run",
                            idempotency_key=req["idempotency_key"],
                        )
                        assert recovered["job_id"] == job["job_id"]
                        conflict = await client.call_tool(
                            "run_backtest",
                            req | {"parameters": {"shares": 300}},
                        )
                        assert conflict.is_error
                        job_id = job["job_id"]
                        run_ids.append(job_id)
                        await asyncio.to_thread(work_one, app.state.tasks, job_id)
                        status = await call(client, "get_run", run_id=job_id)
                        assert status["state"] == "succeeded", status
                        direct = await backend.request(
                            "GET", "/api/v1/python/jobs/" + job_id + "/result"
                        )
                        assert status["summary"] == direct["summary"]
                        assert status["result_sha256"] == digest(direct)
                        assert "metrics" in status and "net_return" in status["metrics"] and "sharpe" in status["metrics"]
                        for section in ("trades", "orders", "strategy_logs", "positions", "equity"):
                            rows = await collect(
                                client,
                                "get_run_details",
                                run_id=job_id,
                                section=section,
                            )
                            if section == "strategy_logs":
                                expected = list(direct["logs"])
                            elif section == "positions":
                                expected = [
                                    {"at": r["at"], "security": s, **p}
                                    for r in direct["native"]["accounts"]
                                    for s, p in r["positions"].items()
                                ]
                            elif section == "equity":
                                expected = [
                                    {k: v for k, v in r.items() if k != "positions"}
                                    for r in direct["native"]["accounts"]
                                ]
                            else:
                                expected = direct["native"][section]
                            assert rows == expected
                        logs = await collect(
                            client, "get_run_details", run_id=job_id, section="system_logs"
                        )
                        assert len(logs) == len(
                            (
                                await backend.request(
                                    "GET", "/api/v1/python/jobs/" + job_id + "/logs"
                                )
                            )["items"]
                        )
                        assert direct["summary"]["trade_count"] == 2
                        receipt["jobs"].append(
                            dict(
                                job_id=job_id,
                                shares=size,
                                summary=direct["summary"],
                                result_sha256=digest(direct),
                            )
                        )
                    assert (
                        receipt["jobs"][0]["summary"]["net_change"]
                        != receipt["jobs"][1]["summary"]["net_change"]
                    )
                    # List and compare runs side-by-side
                    listed = await call(client, "list_runs", kind="python")
                    assert listed["total"] >= 2
                    compared = await call(client, "compare_runs", run_ids=run_ids)
                    assert compared["varying_parameters"] == {"shares": [100, 200]}
                    assert len(compared["runs"]) == 2

                # A fresh subprocess recovers the same durable job; adapter has no private job store.
                async with Client(params, mode="legacy") as client:
                    recovered = await call(
                        client,
                        "get_run",
                        idempotency_key="iteration-100",
                    )
                    assert recovered["job_id"] == receipt["jobs"][0]["job_id"]
                    receipt["stdio_protocol"] = client.protocol_version

            asyncio.run(scenario())
    (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2))
