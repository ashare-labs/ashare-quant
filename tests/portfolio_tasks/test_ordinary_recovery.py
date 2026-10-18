"""Fault/identity checks for explicit inputs produced by the ordinary product.

The caller supplies aq-ordinary-fault-inputs.v1 cases after public publication
and AQ attachment. Historical model fixtures are never substituted here.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.portfolio.inputs import digest as input_digest
from ashare_quant.storage.artifacts import canonical
from ashare_quant.tasks.worker import work_one

P = "/api/v1/portfolio"
NATIVE_STAGES = ("native_submit", "decision_candidate", "before_replace", "after_replace")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cases(cash=False):
    value = os.getenv("ASHARE_QUANT_TEST_ORDINARY_INPUT")
    if not value:
        return [pytest.param(None, marks=pytest.mark.skip(
            reason="Explicit new ordinary product publication inputs required"), id="no-input")]
    manifest = json.loads(Path(value).read_text())
    assert manifest["schema"] == "aq-ordinary-fault-inputs.v1"
    selected = []
    for case in manifest["cases"]:
        assert case["mode"] == "covered_research"
        assert sha(case["input_path"]) == case["input_sha256"]
        if cash:
            document = json.loads(Path(case["input_path"]).read_text())
            if not any(Decimal(e["cash_per_share"]) > 0
                       and case["start_date"] <= e["record_date"]
                       <= e["payment_date"] <= case["end_date"]
                       for e in document["corporate_schedule"]["rows"]):
                continue
        selected.append(pytest.param(case, id=case["name"]))
    assert selected, "Ordinary matrix must contain a real cash window" if cash else "No ordinary cases"
    return selected


def create(host, key):
    response = host["client"].post(P + "/jobs", json=host["request"] | {"idempotency_key": key})
    assert response.status_code == 201, response.text
    return response.json()


def finish(host, job):
    jid = job["job_id"]
    assert work_one(host["app"].state.daily_tasks, jid) == jid
    response = host["client"].get(P + "/jobs/" + jid + "/result")
    assert response.status_code == 200 and response.headers["ETag"], response.text
    result = response.json()
    assert result["audit"]["passed"]
    return result


def same_economics(result, baseline):
    for key in ("days", "orders", "trades", "decisions", "summary", "account"):
        assert result[key] == baseline[key], key
    assert result["receipt"]["corporate_entitlements"] == baseline["receipt"]["corporate_entitlements"]
    assert len({trade["id"] for trade in result["trades"]}) == len(result["trades"])


@pytest.fixture(scope="module")
def ordinary_host(tmp_path_factory):
    hosts = {}

    def get(case):
        if case["name"] in hosts:
            return hosts[case["name"]]
        path = Path(case["input_path"])
        document = json.loads(path.read_text())
        assert sha(path) == case["input_sha256"]
        assert document["real_market_data"] is True
        assert document["product_release"]["schema"] == "aq-product-release.v1"
        assert document["product_release"]["mode"] == "covered_research"
        assert document["product_coverage"]["ready"] is True
        assert document["reference_evidence"] and "corporate_account_model" in document
        identity = document["product_release"]
        for name, field in (("base-input.json", "portfolio_input_sha256"),
                            ("corporate-schedule.json", "corporate_schedule_sha256"),
                            ("ad-manifest.json", "manifest_file_sha256")):
            assert sha(path.parent / name) == identity[field]
        assert json.loads((path.parent / "ad-manifest.json").read_text()) == identity["manifest"]
        assert json.loads((path.parent / "coverage.json").read_text()) == document["product_coverage"]
        assert json.loads((path.parent / "corporate-schedule.json").read_text()) == document["corporate_schedule"]
        assert input_digest(json.loads((path.parent / "base-input.json").read_text())) == identity["portfolio_input_semantic_sha256"]
        assert input_digest(document["corporate_schedule"]) == identity["corporate_schedule_semantic_sha256"]
        root = tmp_path_factory.mktemp("ordinary-" + case["name"])
        app = create_app(str(root / "control.sqlite3"))
        jobs = app.state.portfolio_jobs
        descriptor = jobs.import_data(path, case["name"])
        client = TestClient(app)
        selection = {k: case[k] for k in ("securities", "start_date", "end_date", "mode")}
        selection.update(dataset_id=descriptor.dataset_id, accept_retrospective_state=True)
        reply = client.post(P + "/preflight", json=selection)
        assert reply.status_code == 200 and reply.json()["ready"], reply.text
        version = client.post(P + "/strategies", json={"algorithm": "equal-weight"}).json()
        host = {"app": app, "jobs": jobs, "client": client, "document": document, "case": case,
                "request": selection | {"name": "Ordinary fault check", "idempotency_key": "base",
                    "strategy_version_id": version["version_id"],
                    "assumption_ack_sha256": reply.json()["assumption_ack_sha256"]}}
        host["baseline"] = finish(host, create(host, "baseline"))
        assert host["baseline"]["economic_scope"] == "source_covered_conditional_research"
        assert host["baseline"]["trades"], "Native submit coverage needs actual executed trades"
        hosts[case["name"]] = host
        return host

    yield get
    for host in hosts.values():
        host["client"].close()


def event_cursor(host, job, day, event):
    accepted = host["jobs"].repo.manifest(host["jobs"].repo.row(job["job_id"]))
    records = accepted.session["details"]["records"]
    selected = [r for r in records if r["dt"].startswith(day + "T") and r["event"] == event]
    assert len(selected) == 1
    return selected[0]["cursor"]


@pytest.mark.parametrize("case", cases())
def test_ordinary_base_schedule_and_source_identity_remain_frozen(ordinary_host, case):
    host = ordinary_host(case)
    job = create(host, "provenance")
    root = host["jobs"].directory(job["job_id"])
    before = (root / "accepted-manifest.json").read_bytes()
    frozen = json.loads((root / "input.json").read_text())
    for key in ("product_release", "product_coverage", "reference_evidence",
                "corporate_schedule", "corporate_account_model"):
        assert frozen[key] == host["document"][key], key
    result = finish(host, job)
    same_economics(result, host["baseline"])
    assert (root / "accepted-manifest.json").read_bytes() == before
    assert sha(case["input_path"]) == case["input_sha256"]


@pytest.mark.parametrize("case", cases())
@pytest.mark.parametrize("stage", NATIVE_STAGES)
def test_ordinary_native_sigkill_and_repeat_do_not_duplicate_money(ordinary_host, case, stage):
    host = ordinary_host(case)
    job = create(host, stage)
    day = host["baseline"]["trades"][0]["date"]
    phase = "BEFORE_TRADING" if stage == "decision_candidate" else "BAR"
    fault = f"{stage}:{event_cursor(host, job, day, phase)}"
    assert work_one(host["app"].state.daily_tasks, job["job_id"], _fault=fault) == job["job_id"]
    row = host["jobs"].repo.row(job["job_id"])
    assert row["status"] == "failed" and row["result"] is None
    assert json.loads(row["error"])["returncode"] == -signal.SIGKILL
    host["jobs"].resume(job["job_id"])
    same_economics(finish(host, job), host["baseline"])
    assert host["jobs"].get(job["job_id"]).attempt_count == 2
    checkpoint = host["jobs"].directory(job["job_id"]) / "native.chk"
    before = sha(checkpoint)
    assert work_one(host["app"].state.daily_tasks, job["job_id"]) is None
    assert sha(checkpoint) == before


@pytest.mark.parametrize("case", cases(cash=True))
def test_ordinary_cash_sigkill_recovers_one_exact_entitlement(ordinary_host, case):
    host = ordinary_host(case)
    event = next(e for e in host["document"]["corporate_schedule"]["rows"]
                 if Decimal(e["cash_per_share"]) > 0 and case["start_date"] <= e["record_date"]
                 <= e["payment_date"] <= case["end_date"])
    job = create(host, "cash")
    fault = f"corporate_cash:{event_cursor(host, job, event['payment_date'], 'BEFORE_TRADING')}"
    work_one(host["app"].state.daily_tasks, job["job_id"], _fault=fault)
    row = host["jobs"].repo.row(job["job_id"])
    assert row["status"] == "failed" and json.loads(row["error"])["returncode"] == -signal.SIGKILL
    host["jobs"].resume(job["job_id"])
    result = finish(host, job)
    same_economics(result, host["baseline"])
    entries = result["receipt"]["corporate_entitlements"][event["security"]]
    matching = [entry for entry in entries.values()
                if entry["event"]["entitlement_key"] == event["entitlement_key"]]
    assert len(matching) == 1 and matching[0]["basis"] > 0
    assert Decimal(matching[0]["cash"]) == matching[0]["basis"] * Decimal(event["cash_per_share"])


@pytest.mark.parametrize("case", cases())
def test_ordinary_dead_publisher_recovers_checkpoint_without_new_trades(ordinary_host, case, tmp_path):
    host = ordinary_host(case)
    job = create(host, "dead-publisher")
    marker = tmp_path / "native-complete.json"
    code = """
import os,signal,sys
from pathlib import Path
from ashare_quant.api import create_app
from ashare_quant.tasks.worker import work_one
app=create_app(sys.argv[1])
def stage(name,*_):
    if name=='before_publish':
        Path(sys.argv[3]).write_text('native committed, publication not started')
        os.kill(os.getpid(),signal.SIGKILL)
work_one(app.state.daily_tasks,sys.argv[2],_stage=stage)
"""
    command = [sys.executable, "-B", "-c", code, str(host["jobs"].repo.store.path),
               job["job_id"], str(marker)]
    with (tmp_path / "publisher.log").open("x") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60)
    assert process.returncode == -signal.SIGKILL and marker.exists()
    checkpoint = host["jobs"].directory(job["job_id"]) / "native.chk"
    before = sha(checkpoint)
    assert host["jobs"].get(job["job_id"]).result is None
    deadline = time.monotonic() + 20
    while not host["jobs"].get(job["job_id"]).recovery_required:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    same_economics(finish(host, job), host["baseline"])
    assert sha(checkpoint) == before


@pytest.mark.parametrize("case", cases())
def test_ordinary_concurrent_idempotency_has_one_account(ordinary_host, case):
    host = ordinary_host(case)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = list(pool.map(lambda _: create(host, "concurrent"), range(3)))
    assert len({job["job_id"] for job in jobs}) == 1
    assert len({job["account_id"] for job in jobs}) == 1
    response = host["client"].post(P + "/jobs", json=host["request"] | {
        "idempotency_key": "concurrent", "initial_cash": "2000000"})
    assert response.status_code == 409
    same_economics(finish(host, jobs[0]), host["baseline"])


@pytest.mark.parametrize("case", cases())
@pytest.mark.parametrize("target", ("input", "strategy", "receipt"))
def test_ordinary_frozen_bytes_refuse_rebinding(ordinary_host, case, target):
    host = ordinary_host(case)
    job = create(host, "tamper-" + target)
    root = host["jobs"].directory(job["job_id"])
    if target == "receipt":
        finish(host, job)
        publication = root / host["jobs"].get(job["job_id"]).result.publication
        path = publication / "receipt.json"
        document = json.loads(path.read_text())
        document["final_account"]["cash"] = "999999999"
        path.write_bytes(canonical(document))
        response = host["client"].get(P + "/jobs/" + job["job_id"] + "/result")
        assert response.status_code == 409, response.text
    else:
        if target == "input":
            path = root / "input.json"
            document = json.loads(path.read_text())
            document["product_release"]["portfolio_input_sha256"] = "0" * 64
            path.write_bytes(canonical(document))
        else:
            path = root / "equal-weight/strategy.py"
            path.write_bytes(path.read_bytes() + b"\n# out-of-band mutation\n")
        response = host["client"].get(P + "/jobs/" + job["job_id"] + "/manifest")
        assert response.status_code == 409, response.text
        work_one(host["app"].state.daily_tasks, job["job_id"])
        row = host["jobs"].repo.row(job["job_id"])
        assert row["status"] == "failed" and row["result"] is None
        assert not (root / "native.chk").exists()
    assert sha(case["input_path"]) == case["input_sha256"]
