"""Protocol and closed-input tests; no mock claims of factor integration."""

import asyncio

import httpx
import pytest
from mcp import Client
from pydantic import ValidationError

from ashare_quant.mcp.client import Backend, ResearchError, Settings
from ashare_quant.mcp.contracts import DailySelection, MinuteSelection, StrategySubmission
from ashare_quant.mcp.projection import page
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


def test_bounded_inputs_and_required_consent():
    body = submission("a" * 64)
    for key in ("accept_research_model", "trusted_local_code"):
        for value in (None, False):
            invalid = dict(body)
            if value is None:
                invalid.pop(key)
            else:
                invalid[key] = value
            with pytest.raises(ValidationError):
                StrategySubmission.model_validate(invalid)
    for change in (
        {"path": "/etc/passwd"},
        {"shell": "ls"},
        {"source_path": "/tmp/a.py"},
        {"idempotency_key": "../bad"},
        {"environment_id": "other"},
    ):
        with pytest.raises(ValidationError):
            StrategySubmission.model_validate(body | change)
    with pytest.raises(ValidationError):
        DailySelection(securities=["000001.XSHE"], start="2024-01-01", end="2024-03-01")
    with pytest.raises(ValidationError):
        MinuteSelection(
            security="000001.XSHE",
            start="2024-01-01T00:00:00+08:00",
            end="2024-01-05T00:00:00+08:00",
        )


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


def test_sdk_schema_and_missing_air_contracts_are_honest():
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
        for mode in ("auto", "legacy"):
            async with Client(server, mode=mode) as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                assert len(tools) == 10
                schema = tools["submit_strategy"].input_schema
                fields = schema["$defs"]["StrategySubmission"]
                assert {
                    "trusted_local_code",
                    "accept_research_model",
                    "environment_id",
                    "source",
                } <= set(fields["required"])
                assert fields["additionalProperties"] is False
                assert tools["submit_strategy"].annotations.read_only_hint is False
                caps = (await c.call_tool("research_capabilities")).structured_content
                assert not caps["extensions"]["factors"]["available"]
                assert all(caps["extensions"]["factors"]["discovered_paths"].values())
                assert not caps["extensions"]["python_daily"]["available"]
                for name, args in [
                    ("research_job", {"family": "python_minute", "job_id": "../../etc/passwd"}),
                    (
                        "read_minute_prices",
                        {"configuration_id": "a" * 64, "selection": {"path": "/tmp/a"}},
                    ),
                    (
                        "research_logs",
                        {"family": "daily", "job_id": "m2job-" + "a" * 32, "limit": 201},
                    ),
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
                "submit_strategy", {"submission": submission("a" * 64, "preserve-this-key")}
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
    import json
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
                    200, json={"rows": [{"close": "10", "security": "000001.XSHE"}]}
                )
            return httpx.Response(200, json=["malformed backend response"])

        backend = Backend(
            Settings("http://127.0.0.1:8000"), transport=httpx.MockTransport(response)
        )
        async with Client(create_server(backend)) as c:
            result = await c.call_tool(
                "inspect_data",
                {
                    "configuration_id": "a" * 64,
                    "selection": {
                        "securities": ["000001.XSHE"],
                        "start": "2024-01-01",
                        "end": "2024-01-02",
                    },
                },
            )
            assert not result.is_error
            value = result.structured_content
            assert not value["coverage"]["ready"]
            assert value["coverage"]["error"]["code"] == "REFERENCE_UNAVAILABLE"
            assert value["page"]["items"][0]["close"] == "10"
        with pytest.raises(ResearchError, match="BACKEND_PROTOCOL"):
            await backend.request("GET", "/api/v1/unknown")

    asyncio.run(scenario())
