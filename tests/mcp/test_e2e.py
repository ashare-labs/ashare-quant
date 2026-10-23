"""Real MCP transports -> loopback HTTP -> immutable AD -> queue -> native RQAlpha.

Synthetic engineering observations only. No mocks in the execution/result chain.
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mcp import Client, StdioServerParameters

from ashare_quant.api import create_app
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.projection import digest
from ashare_quant.mcp.server import create_server
from ashare_quant.tasks.worker import work_one
from tests.mcp.fixture import DAYS, SID, make_deployment, submission


@contextmanager
def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result
    assert result.structured_content is not None, result
    return result.structured_content


async def collect(client, name, **args):
    rows, cursor = [], None
    while True:
        result = await call(client, name, **args, limit=1, cursor=cursor)
        value = result.get("page", result)
        rows.extend(value["items"])
        cursor = value["next_cursor"]
        if not cursor:
            assert len(rows) == value["total"]
            return rows


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
                        assert len((await client.list_tools()).tools) == 13
                        caps = await call(client, "research_capabilities")
                        assert caps["python_minute"]["available"]
                        assert all(f["available"] for f in caps["task_families"].values())
                        receipt.setdefault("http_protocols", []).append(client.protocol_version)
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-B", "-m", "ashare_quant.mcp.server", "--backend-url", url],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                async with Client(params) as client:
                    caps = await call(client, "research_capabilities")
                    env = caps["python_minute"]["value"]["environment_id"]
                    cid = caps["product"]["value"]["configuration_id"]
                    assert caps["python_minute"]["value"]["max_stock_minutes"] == 20000
                    assert caps["extensions"]["factors"]["available"]
                    assert caps["extensions"]["python_daily"]["available"]
                    found = await call(client, "discover_data", configuration_id=cid)
                    assert found["catalogue"]["securities"][0]["security"] == SID
                    selection = dict(securities=[SID], start=DAYS[1], end=DAYS[2])
                    inspected = await call(
                        client, "inspect_data", configuration_id=cid, selection=selection
                    )
                    assert inspected["coverage"]["ready"] is False  # no certified limits in fixture
                    direct = await backend.request("POST", "/api/v1/product/prices", body=selection)
                    assert (
                        await collect(
                            client, "inspect_data", configuration_id=cid, selection=selection
                        )
                        == direct["rows"]
                    )
                    minute = dict(
                        security=SID,
                        start=DAYS[1] + "T09:31:00+08:00",
                        end=DAYS[1] + "T09:35:00+08:00",
                    )
                    direct = await backend.request(
                        "POST", "/api/v1/product/minute/prices", body=minute
                    )
                    assert (
                        await collect(
                            client, "read_minute_prices", configuration_id=cid, selection=minute
                        )
                        == direct["rows"]
                    )
                    assert (
                        await client.call_tool(
                            "inspect_data", dict(configuration_id="f" * 64, selection=selection)
                        )
                    ).is_error
                    for size in (100, 200):
                        body = submission(env, key="iteration-" + str(size), shares=size)
                        job = await call(client, "submit_strategy", submission=body)
                        assert job["status"] == "queued" and job["attempt_count"] == 0
                        assert body["source"] not in json.dumps(job)
                        assert "request" not in job
                        same = await call(client, "submit_strategy", submission=body)
                        assert same["job_id"] == job["job_id"]
                        direct_submit = await backend.request(
                            "POST", "/api/v1/python/jobs", body=body
                        )
                        assert direct_submit["job_id"] == job["job_id"]
                        recovered = await call(
                            client,
                            "find_research_job",
                            family="python_minute",
                            idempotency_key=body["idempotency_key"],
                        )
                        assert recovered["job"]["job_id"] == job["job_id"]
                        conflict = await client.call_tool(
                            "submit_strategy",
                            dict(submission=body | {"parameters": {"shares": 300}}),
                        )
                        assert conflict.is_error
                        job_id = job["job_id"]
                        await asyncio.to_thread(work_one, app.state.tasks, job_id)
                        status = await call(
                            client, "research_job", family="python_minute", job_id=job_id
                        )
                        assert status["status"] == "succeeded", status
                        direct = await backend.request(
                            "GET", "/api/v1/python/jobs/" + job_id + "/result"
                        )
                        summary = await call(
                            client, "research_result", family="python_minute", job_id=job_id
                        )
                        assert summary["summary"] == direct["summary"]
                        assert summary["result_sha256"] == digest(direct)
                        assert "native" not in summary
                        for section in ("trades", "orders", "strategy_logs", "positions", "equity"):
                            rows = await collect(
                                client,
                                "research_result",
                                family="python_minute",
                                job_id=job_id,
                                section=section,
                            )
                            if section == "strategy_logs":
                                expected = direct["logs"]
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
                            client, "research_logs", family="python_minute", job_id=job_id
                        )
                        assert (
                            logs
                            == (
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
                # A fresh subprocess recovers the same durable job; adapter has no private job store.
                async with Client(params, mode="legacy") as client:
                    recovered = await call(
                        client,
                        "find_research_job",
                        family="python_minute",
                        idempotency_key="iteration-100",
                    )
                    assert recovered["job"]["job_id"] == receipt["jobs"][0]["job_id"]
                    receipt["stdio_protocol"] = client.protocol_version

            asyncio.run(scenario())
    (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2))
    destination = os.environ.get("ASHARE_MCP_EVIDENCE")
    if destination:
        from pathlib import Path

        Path(destination).write_text(json.dumps(receipt, indent=2))


class LostResponse(httpx.AsyncBaseTransport):
    """Fault only the wire after the *real* backend accepted the POST."""

    def __init__(self):
        self.dropped = False

    async def handle_async_request(self, request):
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
            if (
                request.method == "POST"
                and request.url.path.endswith("/jobs")
                and not self.dropped
                and response.status_code == 201
            ):
                self.dropped = True
                raise httpx.ReadTimeout("Injected response loss after real commit", request=request)
            return response


def test_response_loss_concurrent_idempotency_cancellation_and_failure(tmp_path):
    pytest.importorskip("rqalpha")
    config = make_deployment(tmp_path / "lab")
    app = create_app(product_configuration=config)
    with serving(app) as url:
        normal = Backend(Settings(url))
        dropping = LostResponse()
        server = create_server(Backend(Settings(url), transport=dropping))

        async def scenario():
            async with Client(server) as c:
                env = (await call(c, "research_capabilities"))["python_minute"]["value"][
                    "environment_id"
                ]
                body = submission(env, key="lost-response")
                for invalid in (
                    body | {"environment_id": "f" * 64},
                    body | {"trusted_local_code": False},
                    body | {"accept_research_model": False},
                ):
                    assert (await c.call_tool("submit_strategy", {"submission": invalid})).is_error
                assert (await normal.request("GET", "/api/v1/python/jobs"))["items"] == []
                lost = await call(c, "submit_strategy", submission=body)
                assert dropping.dropped and lost["recovered_after_timeout"]
                repeated = await asyncio.gather(
                    *[call(c, "submit_strategy", submission=body) for _ in range(3)]
                )
                assert {j["job_id"] for j in repeated} == {lost["job_id"]}
                assert len((await normal.request("GET", "/api/v1/python/jobs"))["items"]) == 1
                cancel = await call(
                    c, "cancel_research_job", family="python_minute", job_id=lost["job_id"]
                )
                assert cancel["status"] == "cancelled" and not cancel["can_resume"]
                assert (
                    await c.call_tool(
                        "research_result", dict(family="python_minute", job_id=lost["job_id"])
                    )
                ).is_error
                bad = await call(
                    c,
                    "submit_strategy",
                    submission=submission(
                        env,
                        "failure",
                        source='def initialize(context):\n    raise ValueError("intentional engineering failure")\n',
                    ),
                )
                await asyncio.to_thread(work_one, app.state.tasks, bad["job_id"])
                state = await call(c, "research_job", family="python_minute", job_id=bad["job_id"])
                assert state["status"] == "failed" and state["error"]
                assert "intentional engineering failure" in json.dumps(state["error"])
                slow = await call(
                    c,
                    "submit_strategy",
                    submission=submission(
                        env,
                        "running-cancel",
                        source="import time\ndef initialize(context):\n    time.sleep(30)\n",
                    ),
                )
                work = asyncio.create_task(
                    asyncio.to_thread(work_one, app.state.tasks, slow["job_id"])
                )
                try:
                    for _ in range(100):
                        status = await call(
                            c, "research_job", family="python_minute", job_id=slow["job_id"]
                        )
                        if status["status"] == "running":
                            break
                        await asyncio.sleep(0.03)
                    assert status["status"] == "running"
                    cancelled = await call(
                        c, "cancel_research_job", family="python_minute", job_id=slow["job_id"]
                    )
                    assert cancelled["status"] in ("cancelling", "cancelled")
                    await asyncio.wait_for(work, 15)
                    state = await call(
                        c, "research_job", family="python_minute", job_id=slow["job_id"]
                    )
                    assert state["status"] == "cancelled" and not state["can_resume"]
                finally:
                    if not work.done():
                        await normal.request(
                            "POST", "/api/v1/python/jobs/" + slow["job_id"] + "/cancel", body={}
                        )
                        await work

        asyncio.run(scenario())
