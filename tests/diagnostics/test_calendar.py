"""Ordinary product checks using an explicit copied real owner Store; no market synthesis."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.diagnostics.contracts import DiagnosticQuery
from ashare_quant.diagnostics.service import Diagnostics
from tests.support.legacy_provider.owner_wire import verify_runtime


@pytest.fixture(scope="module")
def configuration():
    path = os.getenv("ASHARE_QUANT_TEST_DIAGNOSTICS_CONFIG")
    if not path:
        pytest.skip("Requires the paired installed AD and a copied real calendar capture")
    return json.loads(Path(path).read_text())


def query(dates=None, **update):
    return DiagnosticQuery.model_validate({"source_alias": "calendar-r1", "security": "600000.XSHG",
                                           "trading_dates": dates or ["2026-09-29"], **update})


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_public_calendar_and_three_capabilities_do_not_admit_execution(configuration):
    store = Path(configuration["calendar-r1"]["store"])
    before = inventory(store)
    report = Diagnostics(configuration).diagnose(query())
    assert report.calendar.status == report.calendar_links.status == "readable"
    row = report.calendar_links.public_result["rows"][0]
    assert (row["previous_open"], row["trading_date"], row["next_open"]) == ("2026-09-28", "2026-09-29", "2026-09-30")
    assert len(row["evidence_rows"]) == 3
    assert all(r["raw_record_sha256"] and r["raw_response_sha256"] for r in row["evidence_rows"])
    assert report.research_readable == "partial_research_values"
    assert report.research_preflight.status == "readable"
    assert report.research_values_complete is False
    assert report.initialization.status == "not_admitted"
    assert not report.initialization.requires_user_approval and not report.initialization.execution_permission
    assert report.trading_facts.status == "unknown" and report.trading_facts.security_eligibility is None
    assert not row["historical_pit"] and not row["execution_permission"]
    assert report.read_completed_at.tzinfo and not report.network_used
    assert inventory(store) == before
    verify_runtime()  # Calendar and execution reads share the paired installed owner.


@pytest.mark.parametrize("date,code,direction", [
    ("2026-09-28", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN", "previous"),
    ("2026-09-30", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN", "next"),
    ("2026-10-09", "SOURCE_CALENDAR_OUT_OF_SCOPE", None),
])
def test_calendar_readable_is_distinct_from_supported_anchor(configuration, date, code, direction):
    report = Diagnostics(configuration).diagnose(query([date]))
    assert report.calendar.status == "readable"
    assert report.calendar_links.status == "refused"
    assert report.calendar_links.public_result is None
    error = report.calendar_links.error
    assert error.origin == "ashare-data" and error.code == code
    assert error.details["date"] == date
    if direction:
        assert error.details["direction"] == direction
    assert report.trading_facts.status == "unknown"


def test_mixed_anchors_preserve_owner_atomic_refusal(configuration):
    report = Diagnostics(configuration).diagnose(query(["2026-09-29", "2026-10-09"]))
    assert report.calendar_links.public_result is None
    assert report.calendar_links.error.code == "SOURCE_CALENDAR_OUT_OF_SCOPE"


def test_security_is_request_context_not_calendar_eligibility(configuration):
    report = Diagnostics(configuration).diagnose(query(security="300750.XSHE"))
    assert report.query.security == "300750.XSHE"
    assert report.calendar_links.status == "readable"
    assert report.research_values_complete is False
    assert all(row["security"] == "300750.XSHE" for row in report.research_preflight.public_result["rows"])
    assert report.trading_facts.security_eligibility is None


def test_missing_capture_preserves_data_owner_error(configuration):
    config = {"calendar-r1": {**configuration["calendar-r1"], "calendar_capture_id": "0" * 64}}
    report = Diagnostics(config).diagnose(query())
    assert report.research_readable == "blocked"
    assert report.calendar.status == report.calendar_links.status == "refused"
    assert report.calendar.error.origin == "ashare-data"
    assert report.calendar.error == report.calendar_links.error
    assert report.calendar.public_result is None


def test_paired_environment_is_used_as_calendar_owner(configuration):
    config = {"calendar-r1": {**configuration["calendar-r1"], "python": sys.executable}}
    assert Diagnostics(config).diagnose(query()).calendar.status == "readable"
    verify_runtime()


def test_missing_local_runtime_is_explicit(configuration, tmp_path):
    config = {"calendar-r1": {**configuration["calendar-r1"], "python": str(tmp_path / "missing-python")}}
    with pytest.raises(DomainError) as exc:
        Diagnostics(config).diagnose(query())
    assert exc.value.code == "DIAGNOSTIC_RUNTIME_UNAVAILABLE"


def test_http_report_preserves_public_owner_evidence_and_creates_no_jobs(configuration, tmp_path):
    with TestClient(create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration=configuration)) as client:
        caps = client.get("/api/v1/source-diagnostics/capabilities").json()
        assert caps["contract_version"] == "source-diagnostics.v1"
        assert caps["configured_sources"] == ["calendar-r1"]
        before = client.get("/api/v1/tasks/daily/jobs").json()
        result = client.post("/api/v1/source-diagnostics", json=query().model_dump(mode="json"))
        assert result.status_code == 200, result.text
        assert result.json()["initialization"]["execution_permission"] is False
        assert client.get("/api/v1/tasks/daily/jobs").json() == before
        assert client.get("/api/v1/tasks/daily/jobs/catalogue").json()["contract_version"] == "m2-jobs.v6"
        bad = client.post("/api/v1/source-diagnostics", json=query().model_dump(mode="json") | {"store": "/tmp"})
        assert bad.status_code == 422
        assert "/api/v1/source-diagnostics" in client.get("/api/v1/openapi.json").json()["paths"]


def test_cli_report_does_not_create_control_database(configuration, tmp_path):
    request = tmp_path / "request.json"
    request.write_text(query().model_dump_json())
    db = tmp_path / "must-not-exist.sqlite3"
    env = os.environ | {"ASHARE_QUANT_DIAGNOSTICS": json.dumps(configuration)}
    result = subprocess.run([sys.executable, "-m", "ashare_quant.cli", "--db", str(db),
                             "source-diagnostics", "--request", str(request)], capture_output=True, text=True, env=env, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["research_readable"] == "partial_research_values"
    assert value["research_preflight"]["status"] == "readable"
    assert value["research_values_complete"] is False
    assert not db.exists()


@pytest.mark.parametrize("dates", [["2026-09-29T00:00:00+08:00"], [1], [True], ["20260929"],
                                    ["2026-09-29", "2026-09-29"], ["2026-09-01", "2026-10-02"]])
def test_dates_are_exact_unique_and_bounded(dates):
    with pytest.raises(ValidationError):
        query(dates)


def test_unconfigured_capability_and_error_require_no_owner_runtime():
    diagnostics = Diagnostics({})
    assert diagnostics.capabilities().configured_sources == ()
    with pytest.raises(DomainError) as exc:
        diagnostics.diagnose(query())
    assert exc.value.code == "DIAGNOSTIC_SOURCE_NOT_CONFIGURED"


def test_transport_protocol_mismatch_is_not_reported_as_owner_data(monkeypatch, configuration):
    from ashare_quant.diagnostics import service
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, '{"protocol":"wrong"}', ""))
    with pytest.raises(DomainError) as exc:
        Diagnostics(configuration).diagnose(query())
    assert exc.value.code == "DIAGNOSTIC_PROTOCOL_MISMATCH"


def complete_query(**update):
    example = Path(__file__).parents[2] / "tests/fixtures/research/diagnostics/preflight-600000.json"
    return DiagnosticQuery.model_validate(json.loads(example.read_text()) | update)


def test_real_preflight_preserves_values_units_roles_and_blocks_execution(configuration):
    report = Diagnostics(configuration).diagnose(complete_query())
    assert report.research_readable == "complete_research_values"
    assert report.research_values_complete is True
    result = report.research_preflight.public_result
    assert result["report_id"] == "a3ab1c866c6bbbf8027ad917cdc9cc5e80cb9379ba6143e52958f0195462a5eb"
    rows = {r["role"]: r for r in result["rows"]}
    anchor = {f["field"]: f for f in rows["anchor_day_research"]["facts"]}
    assert anchor["close"]["value"] == "9.1800"
    assert anchor["source_preclose"]["value"] == "9.1600"
    assert anchor["amount"]["value"] == "739740988.4800"
    assert anchor["volume"]["value"] == 80509739
    assert anchor["close"]["unit"] == anchor["source_preclose"]["unit"] == "CNY/share"
    assert anchor["volume"]["unit"] == "share" and anchor["amount"]["unit"] == "CNY"
    assert anchor["suspended"]["value"] is False and anchor["is_st"]["value"] is False
    assert not any(f["field"] in {"open", "high", "low", "close", "amount", "volume"}
                   for f in rows["successor_metadata"]["facts"])
    assert result["execution_permission"] is False and result["historical_pit"] is False
    assert result["execution_admission"] == "BLOCKED_NO_OWNER_PLAN"
    assert anchor["official_price_limits"]["state"] == "unsupported"
    assert report.initialization.initialization_read_domain == "no_execution_owner_binding"
    verify_runtime()


def test_strict_missing_inputs_preserves_incomplete_owner_report(configuration):
    report = Diagnostics(configuration).diagnose(query(require_complete=True))
    assert report.calendar.status == "readable"
    assert report.research_preflight.status == "refused"
    assert report.research_preflight.error.code == "PREFLIGHT_INCOMPLETE"
    assert report.research_preflight.error.origin == "ashare-data"
    assert report.research_values_complete is None
    assert report.research_preflight.error.details
    assert not report.initialization.execution_permission


def test_wrong_security_binding_remains_owner_error(configuration):
    report = Diagnostics(configuration).diagnose(complete_query(security="000001.XSHE"))
    assert report.research_preflight.error.code == "PREFLIGHT_BINDING_MISMATCH"
    assert report.research_values_complete is None
    assert report.trading_facts.status == "unknown"


def test_input_refs_cannot_supply_server_paths_or_coerce_strict_flag():
    with pytest.raises(ValidationError):
        complete_query(price={"kind": "baostock", "version_id": "a" * 64, "store_path": "/tmp"})
    with pytest.raises(ValidationError):
        complete_query(require_complete=1)
