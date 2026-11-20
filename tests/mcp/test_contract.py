"""Protocol, normalization, static preflight, and closed-input tests for MCP v2."""

import asyncio
import json

import httpx
import pytest
from mcp import Client

from ashare_quant.mcp.client import Backend, ResearchError, Settings
from ashare_quant.mcp.contracts import (
    auto_idempotency_key,
    normalize_daily_window,
    normalize_minute_window,
    normalize_securities,
    normalize_security,
)
from ashare_quant.mcp.projection import clean_traceback, compact, digest, page, run_view
from ashare_quant.mcp.server import create_server
from tests.mcp.fixture import submission


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000/path",
        "http://u:p@127.0.0.1:8000",
        "file:///tmp/x",
        "http://127.0.0.1:8000?x=1",
        "http://127.0.0.1:8000#x",
        "http://0.0.0.0:8000",
    ],
)
def test_backend_origin_is_operator_configured_loopback_only(url):
    with pytest.raises(ValueError):
        Settings(url)


def test_security_and_window_normalization_and_bounds():
    assert normalize_security("000001.SZ") == "000001.XSHE"
    assert normalize_security("sz000001") == "000001.XSHE"
    assert normalize_security("000001") == "000001.XSHE"
    assert normalize_security("600000.SH") == "600000.XSHG"
    assert normalize_security("sh600000") == "600000.XSHG"
    assert normalize_security("600000") == "600000.XSHG"
    assert normalize_security("830799.BJ") == "BJ.830799"
    assert normalize_securities("000001.SZ, 600000.SH, 000001.XSHE") == [
        "000001.XSHE",
        "600000.XSHG",
    ]
    with pytest.raises(ResearchError, match="INVALID_SECURITY_CODE"):
        normalize_security("AAPL")

    assert normalize_daily_window("2024-03-01", "2024-03-06") == ("2024-03-01", "2024-03-06")
    with pytest.raises(ResearchError, match="DATE_WINDOW_TOO_WIDE"):
        normalize_daily_window("2024-01-01", "2024-03-01")

    assert normalize_minute_window("2024-03-04", "2024-03-04") == (
        "2024-03-04T09:30:00+08:00",
        "2024-03-04T15:00:00+08:00",
    )
    assert normalize_minute_window("2024-03-04T09:31:00", "2024-03-04T10:00") == (
        "2024-03-04T09:31:00+08:00",
        "2024-03-04T10:00:00+08:00",
    )
    with pytest.raises(ResearchError, match="MINUTE_WINDOW_TOO_WIDE"):
        normalize_minute_window("2024-01-01T00:00:00+08:00", "2024-01-05T00:00:00+08:00")

    k1 = auto_idempotency_key("bt", {"a": 1, "b": 2})
    k2 = auto_idempotency_key("bt", {"b": 2, "a": 1})
    k3 = auto_idempotency_key("bt", {"a": 1, "b": 3})
    assert k1 == k2 and k1 != k3


def test_project_submission_and_projection_bind_the_complete_source_map():
    identity = "aqproj-" + "1" * 32
    project = {
        "project_id": identity,
        "entrypoint": "main.py",
        "files": {"main.py": "from .helper import value\n", "helper.py": "value = 1\n"},
    }
    frozen = dict(
        job_id="m2job-" + "2" * 32,
        status="queued",
        request=dict(
            name="Project test",
            idempotency_key="proj-1",
            strategy_id=identity,
            project=project,
            start_date="2024-03-04",
            end_date="2024-03-05",
            frequency="1m",
        ),
    )
    view = run_view(frozen)["submission"]
    assert "environment_id" not in view
    assert view["strategy_id"] == identity
    assert view["project"] == {
        "project_id": identity,
        "entrypoint": "main.py",
        "file_count": 2,
        "source_sha256": digest(project),
    }
    changed = project | {"files": project["files"] | {"helper.py": "value = 2\n"}}
    changed_frozen = frozen | {"request": frozen["request"] | {"project": changed}}
    assert run_view(changed_frozen)["submission"]["project"] != view["project"]


def test_path_sanitization_and_clean_traceback():
    user_home = "/" + "Users/alice"
    tb = (
        'Traceback (most recent call last):\n'
        f'  File "{user_home}/ashare-quant/src/ashare_quant/backtests/minute/native.py", line 505, in invoke\n'
        '    result = callback(self.api.context)\n'
        '  File "/private/var/folders/xx/tmp123/strategy.py", line 5, in trade\n'
        '    x = 1 / 0\n'
        'ZeroDivisionError: division by zero\n'
    )
    frames = clean_traceback(tb)
    assert frames == [
        "strategy.py:5 in trade -> x = 1 / 0",
        "ZeroDivisionError: division by zero",
    ]
    cleaned = compact({"root": "/secret", "note": f"stored in {user_home}/secret/db.sqlite"})
    assert "root" not in cleaned
    assert user_home not in json.dumps(cleaned)


def test_pages_are_complete_and_tied_to_snapshot_and_query():
    rows = [{"row": n} for n in range(453)]
    seen, cursor = [], None
    while True:
        p = page(rows, ["identity"], 97, cursor)
        seen += p["items"]
        cursor = p["next_cursor"]
        if not cursor:
            break
    assert seen == rows
    cursor = page(rows, ["identity"], 1)["next_cursor"]
    for changed, identity in ((rows + [{"row": 454}], ["identity"]), (rows, ["other"])):
        with pytest.raises(ResearchError, match="PAGE_CHANGED"):
            page(changed, identity, 1, cursor)
    for cursor in ("not base64!", "e30=", "bnVsbA=="):
        with pytest.raises(ResearchError):
            page(rows, [], 1, cursor)


def test_no_confirmation_or_hash_fields_and_honest_discovery():
    async def scenario():
        def response(request):
            if request.url.path == "/api/v1/openapi.json":
                return httpx.Response(
                    200,
                    json={
                        "paths": {
                            "/api/v1/factors/catalogue": {},
                            "/api/v1/factors/sources": {},
                            "/api/v1/factors/jobs": {},
                        }
                    },
                )
            return httpx.Response(
                404, json={"error": {"code": "ABSENT", "message": "not configured"}}
            )

        server = create_server(
            Backend(Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(response))
        )
        banned_fields = {
            "trusted_local_code",
            "accept_research_model",
            "configuration_id",
            "environment_id",
            "family",
        }
        for mode in ("auto", "legacy"):
            async with Client(server, mode=mode) as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                assert len(tools) == 12
                for t in tools.values():
                    props = set(t.input_schema.get("properties", {}).keys())
                    assert not (props & banned_fields), (t.name, props & banned_fields)
                assert tools["run_backtest"].annotations.read_only_hint is False
                env_info = (await c.call_tool("get_environment", {"detail": True})).structured_content
                assert not env_info["ready"]
                assert not env_info["extensions"]["factors"]["available"]
                assert all(env_info["extensions"]["factors"]["discovered_paths"].values())
                assert not env_info["extensions"]["python_daily"]["available"]

                # Resources and prompts work without a live backend
                resources = (await c.list_resources()).resources
                assert any(str(r.uri) == "aquant://api-reference" for r in resources)
                api_doc = await c.read_resource("aquant://api-reference")
                assert "attribute_history" in api_doc.contents[0].text
                tmpl = await c.read_resource("aquant://templates/daily_rebalance")
                assert "def initialize(context):" in tmpl.contents[0].text
                prompt = await c.get_prompt("research_loop", {"goal": "momentum"})
                assert "run_backtest" in prompt.messages[0].content.text

                # Static check_strategy catches syntax, jqdata, unsupported calls, missing initialize
                bad_check = (
                    await c.call_tool(
                        "check_strategy",
                        {
                            "source": "from jqdata import *\ndef foo(context):\n    get_fundamentals(query())\n    log.set_level('order', 'error')\n    s = '000001.SZ'\n"
                        },
                    )
                ).structured_content
                assert not bad_check["valid"]
                codes = {i["code"] for i in bad_check["issues"]}
                assert {
                    "AQUANT_LEGACY_IMPORT",
                    "AQUANT_UNSUPPORTED_CALL",
                    "AQUANT_INITIALIZE_REQUIRED",
                    "NON_CANONICAL_SECURITY_LITERAL",
                } <= codes

                good_check = (
                    await c.call_tool(
                        "check_strategy",
                        {"source": tmpl.contents[0].text, "securities": ["000001.SZ"]},
                    )
                ).structured_content
                assert good_check["valid"]
                assert good_check["normalized_securities"] == ["000001.XSHE"]

                for name, args in [
                    ("get_run", {"run_id": "../../etc/passwd"}),
                    ("get_prices", {"securities": ["INVALID"], "start": "2024-01-01", "end": "2024-01-02"}),
                    ("get_run_details", {"run_id": "m2job-" + "a" * 32, "limit": 201}),
                ]:
                    assert (await c.call_tool(name, args)).is_error

    asyncio.run(scenario())


def test_no_redirect_following_and_bounded_network_failures():
    async def scenario():
        calls = []

        def redirect(request):
            calls.append(str(request.url))
            return httpx.Response(307, headers={"location": "http://example.com/private"}, json={})

        backend = Backend(
            Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(redirect)
        )
        with pytest.raises(ResearchError):
            await backend.request("GET", "/api/v1/openapi.json")
        assert calls == ["http://127.0.0.1:8000/api/v1/openapi.json"]

    asyncio.run(scenario())


def test_ambiguous_submission_never_posts_again_and_reports_key():
    async def scenario():
        posts = []

        def fail(request):
            if request.method == "POST":
                posts.append(request)
            raise httpx.ReadTimeout("wire fault", request=request)

        server = create_server(
            Backend(Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(fail))
        )
        async with Client(server) as c:
            result = await c.call_tool(
                "run_backtest", submission("preserve-this-key")
            )
            assert result.is_error and len(posts) == 1
            assert "SUBMISSION_UNCERTAIN" in str(result.content)
            assert "preserve-this-key" in str(result.content)

    asyncio.run(scenario())


def test_response_byte_limit_and_variable_row_pages(monkeypatch):
    from ashare_quant.mcp import client as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 10)

    async def scenario():
        backend = Backend(
            Settings("http://127.0.0.1:8000"),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"large": "x" * 20})),
        )
        with pytest.raises(ResearchError, match="BACKEND_RESPONSE_LIMIT"):
            await backend.request("GET", "/api/v1/product/catalogue")

    asyncio.run(scenario())
    rows = [{"x": "a" * 40000} for _ in range(3)]
    p = page(rows, "test", 200)
    assert len(p["items"]) == 1 and p["next_cursor"]
    assert page(rows, "test", 200, p["next_cursor"])["offset"] == 1
    with pytest.raises(ResearchError, match="ROW_TOO_LARGE"):
        page([{"x": "a" * 70000}], "test")


def test_checked_in_tool_contract_matches_sdk():
    from pathlib import Path

    from scripts.export_mcp import document

    expected = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/contracts/research-mcp.tools.json").read_text()
    )
    assert asyncio.run(document()) == expected


def test_unavailable_coverage_preserves_error_without_hiding_readable_prices():
    async def scenario():
        def response(request):
            if request.url.path.endswith("/config"):
                return httpx.Response(200, json={"configured": True, "configuration_id": "a" * 64})
            if request.url.path.endswith("/coverage"):
                return httpx.Response(
                    503,
                    json={
                        "error": {
                            "code": "REFERENCE_UNAVAILABLE",
                            "message": "Configure references",
                        }
                    },
                )
            if request.url.path.endswith("/prices"):
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {"date": "2024-01-02", "close": "10", "open": "9.5", "security": "000001.XSHE"}
                        ]
                    },
                )
            return httpx.Response(200, json=["malformed backend response"])

        backend = Backend(
            Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(response)
        )
        async with Client(create_server(backend)) as c:
            result = await c.call_tool(
                "get_prices",
                {
                    "securities": "000001.SZ",
                    "start": "2024-01-01",
                    "end": "2024-01-02",
                    "fields": ["close"],
                },
            )
            assert not result.is_error
            value = result.structured_content
            assert not value["coverage"]["ready"]
            assert value["coverage"]["error"]["code"] == "REFERENCE_UNAVAILABLE"
            assert value["page"]["items"][0] == {
                "date": "2024-01-02",
                "close": "10",
                "security": "000001.XSHE",
            }
        with pytest.raises(ResearchError, match="BACKEND_PROTOCOL"):
            await backend.request("GET", "/api/v1/unknown")

    asyncio.run(scenario())


def test_wait_seconds_polls_until_terminal_state():
    async def scenario():
        job_id = "m2job-" + "c" * 32
        polls = 0

        def response(request):
            nonlocal polls
            if request.url.path == f"/api/v1/history/jobs/{job_id}":
                polls += 1
                status = "running" if polls < 3 else "succeeded"
                return httpx.Response(
                    200,
                    json={
                        "job_id": job_id,
                        "family": "python",
                        "status": status,
                        "display_status": status,
                        "request": {"frequency": "1d", "idempotency_key": "poll-key"},
                    },
                )
            if request.url.path == f"/api/v1/python/jobs/{job_id}":
                status = "running" if polls < 3 else "succeeded"
                return httpx.Response(
                    200,
                    json={
                        "job_id": job_id,
                        "status": status,
                        "display_status": status,
                        "request": {"frequency": "1d", "idempotency_key": "poll-key"},
                    },
                )
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "not found"}})

        backend = Backend(
            Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(response)
        )
        async with Client(create_server(backend)) as c:
            res = await c.call_tool("get_run", {"run_id": job_id, "wait_seconds": 5})
            assert not res.is_error
            assert res.structured_content["state"] == "succeeded"
            assert polls >= 3

    asyncio.run(scenario())
