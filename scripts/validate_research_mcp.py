"""Temporary loopback/stdio acceptance against operator-selected existing releases.

Plan JSON: market_release (root/release_id/manifest_sha256), factor_sources,
factor_requests and optional python_requests. Python requests must contain both
explicit user confirmations. This script does not infer consent or collect data.
Only the new output directory receives deployment state, frozen jobs and one receipt.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from mcp import Client, StdioServerParameters

from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize
from ashare_quant.identity import source_sha256
from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.contracts import FactorSubmission, StrategySubmission
from ashare_quant.mcp.projection import digest, result_rows, sections
from ashare_quant.mcp.server import create_server
from ashare_quant.tasks.worker import work_one


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
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Temporary server failed to start")
            time.sleep(0.01)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Temporary server did not exit")


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise AssertionError(str(result.content))
    assert result.structured_content is not None
    return result.structured_content


async def collect(client, name, **arguments):
    rows, cursor = [], None
    while True:
        value = await call(client, name, **arguments, limit=200, cursor=cursor)
        page = value.get("page", value)
        rows.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            assert len(rows) == page["total"]
            return rows


def deployment(root, pin):
    from ashare_data.storage.local_daily import LocalDailyStore

    # The profile requires an initialized local_daily asset; the explicit release
    # supplies every market observation. This empty asset is never a data fallback.
    LocalDailyStore(root / "empty-daily").close()
    document = dict(
        schema="ashare-deployment.v1",
        data_root=".",
        assets={
            "daily": dict(kind="local_daily", path="empty-daily", role="market"),
            "market": dict(kind="market_release", path=pin["root"], role="market"),
            "releases": dict(kind="store", path="releases", role="publication"),
        },
        profiles={
            "product": dict(
                kind="product",
                daily_asset="daily",
                release_store_asset="releases",
                control_db="state/control.sqlite3",
                paper_root="state/paper",
                market_release=dict(
                    asset="market",
                    release_id=pin["release_id"],
                    manifest_sha256=pin["manifest_sha256"],
                ),
                reference_sets={"formal": dict(kind="market_release")},
                default_reference_set="formal",
                default_minute_source="canonical",
            )
        },
    )
    path = root / "deployment.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    config = resolve(path)
    initialize(config)
    return config


async def exercise(client, backend, app, request, family):
    tool = "submit_factor" if family == "factors" else "submit_strategy"
    prefix = "/api/v1/factors" if family == "factors" else "/api/v1/python"
    model = FactorSubmission if family == "factors" else StrategySubmission
    body = model.model_validate(request).model_dump(mode="json")
    job = await call(client, tool, submission=body)
    job_id = job["job_id"]
    assert (await call(client, tool, submission=body))["job_id"] == job_id
    assert (await backend.request("POST", prefix + "/jobs", body=body))["job_id"] == job_id
    assert (
        await call(
            client, "find_research_job", family=family, idempotency_key=body["idempotency_key"]
        )
    )["job"]["job_id"] == job_id
    await asyncio.to_thread(work_one, app.state.tasks, job_id)
    state = await call(client, "research_job", family=family, job_id=job_id)
    logs = await collect(client, "research_logs", family=family, job_id=job_id)
    direct_logs = await backend.request("GET", prefix + "/jobs/" + job_id + "/logs")
    assert logs == direct_logs["items"]
    receipt = dict(
        family=family,
        job_id=job_id,
        status=state["status"],
        submission_sha256=digest(body),
        error=state.get("error"),
        logs_equal=True,
        idempotent=True,
    )
    if state["status"] != "succeeded":
        receipt["failure_preserved"] = True
        return receipt
    direct = await backend.request("GET", prefix + "/jobs/" + job_id + "/result")
    summary = await call(client, "research_result", family=family, job_id=job_id)
    assert summary["summary"] == direct["summary"]
    assert summary["result_sha256"] == digest(direct)
    checked = {}
    for section in sections(direct, family):
        rows = await collect(
            client, "research_result", family=family, job_id=job_id, section=section
        )
        assert rows == result_rows(direct, family, section), section
        checked[section] = dict(rows=len(rows), sha256=digest(rows))
    # Independent direct-HTTP job verifies values, not only the same result projection.
    control = body | {"idempotency_key": body["idempotency_key"] + "-direct"}
    other = await backend.request("POST", prefix + "/jobs", body=control)
    await asyncio.to_thread(work_one, app.state.tasks, other["job_id"])
    comparison = await backend.request("GET", prefix + "/jobs/" + other["job_id"] + "/result")
    assert comparison["summary"] == direct["summary"]
    for section in sections(direct, family):
        assert result_rows(comparison, family, section) == result_rows(direct, family, section), (
            section
        )
    return receipt | dict(
        summary=direct["summary"],
        result_sha256=digest(direct),
        checked_pages=checked,
        independent_direct_job=other["job_id"],
        independent_direct_values_equal=True,
        factor=direct.get("factor"),
        provenance=direct.get("provenance"),
        evaluation_coverage=(direct.get("evaluation") or {}).get("coverage", {}).get("by_horizon"),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    # Validate consent before creating anything. There are deliberately no default
    # true flags or migration from previously accepted requests.
    for body in plan.get("python_requests", []):
        StrategySubmission.model_validate(body | {"environment_id": "0" * 64})
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for key in tuple(os.environ):
        if key.startswith("ASHARE_"):
            os.environ.pop(key)
    os.environ.update(XDG_CONFIG_HOME=str(root / "config"), PYTHONDONTWRITEBYTECODE="1")
    config = deployment(root, plan["market_release"])
    app = create_app(product_configuration=config, factor_sources=plan["factor_sources"])
    receipt = dict(source_sha256=source_sha256(), plan_sha256=digest(plan), jobs=[])
    with serving(app) as url:
        backend = Backend(Settings(url, 120))
        server = create_server(backend)
        with serving(
            server.streamable_http_app(stateless_http=True, json_response=True)
        ) as mcp_url:

            async def scenario():
                for mode in ("auto", "legacy"):
                    async with Client(mcp_url + "/mcp", mode=mode) as client:
                        assert len((await client.list_tools()).tools) == 13
                        receipt.setdefault("http_protocols", []).append(client.protocol_version)
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-B",
                        "-m",
                        "ashare_quant.mcp.server",
                        "--backend-url",
                        url,
                        "--timeout",
                        "120",
                    ],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                for transport in (params, mcp_url + "/mcp"):
                    async with Client(transport) as client:
                        caps = await call(client, "research_capabilities")
                        receipt["capabilities"] = caps
                        env = caps["extensions"]["factors"]["environment_id"]
                        catalogue = await collect(client, "discover_factors", environment_id=env)
                        assert len(catalogue) == 368
                        receipt["catalogue"] = dict(count=len(catalogue), sha256=digest(catalogue))
                        receipt["sources"] = await collect(
                            client, "factor_sources", environment_id=env
                        )
                        # Split actual jobs across stdio and HTTP; no duplicate transports runs.
                        factor_requests = (
                            plan["factor_requests"][::2]
                            if transport == params
                            else plan["factor_requests"][1::2]
                        )
                        for body in factor_requests:
                            item = next(x for x in catalogue if x["id"] == body["factor_id"])
                            assert item["definition"] == body["definition"]
                            receipt["jobs"].append(
                                await exercise(
                                    client, backend, app, body | {"environment_id": env}, "factors"
                                )
                            )
                        if transport == params:
                            pyenv = caps["extensions"]["python_daily"]["environment_id"]
                            for body in plan.get("python_requests", []):
                                receipt["jobs"].append(
                                    await exercise(
                                        client,
                                        backend,
                                        app,
                                        body | {"environment_id": pyenv},
                                        "python_daily",
                                    )
                                )
                # Reopening the same service restores exact saved results, with no worker.
                restored = create_app(
                    product_configuration=config, factor_sources=plan["factor_sources"]
                )
                for job in receipt["jobs"]:
                    if job["status"] == "succeeded":
                        service = (
                            restored.state.factor_jobs
                            if job["family"] == "factors"
                            else restored.state.python_jobs
                        )
                        assert (
                            digest(service.result(job["job_id"]).model_dump(mode="json"))
                            == job["result_sha256"]
                        )
                receipt["restored_results_equal"] = True

            asyncio.run(scenario())
    receipt["temporary_servers_stopped"] = True
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(root), "jobs": len(receipt["jobs"]), "servers_stopped": True}))


if __name__ == "__main__":
    main()
