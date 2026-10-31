"""Actual SIGKILL boundaries using a caller-supplied, real Longi distribution.

This is mature native-account regression. It does not claim that the new
ordinary product has reference coverage for Longi or every historical date.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from ashare_quant.identity import package_root

INPUT_SHA256 = "72f693a886292822e33ba7848a3499ab03864adc6dbbc16699c65891c81d0e39"
MANIFEST_SHA256 = "1ca032fff32799581391b12428ecef3086a88a815079103985715862ac2b0139"
SID = "601012.XSHG"
FAULT_STAGES = (
    "corporate_record", "corporate_ex", "corporate_cash",
    "corporate_stock_credit", "corporate_stock_tradable",
    "decision_candidate", "before_replace", "after_replace",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def invoke(source, output, invocation, fault="none"):
    output.mkdir(parents=True, exist_ok=True)
    arguments = [
        "--manifest", str(source / "manifest.json"),
        "--input", str(source / "input.json"),
        "--strategy", "daily-equal-weight", "--window", "test",
        "--output", str(output), "--invocation", invocation,
        "--accept-owner-state-model", "--fault", fault,
    ]
    # Enter the CLI's isolated branch directly so the observed returncode is
    # the native process's -SIGKILL, not the outer CLI's modulo-256 exit code.
    boot = (
        "import json,sys;paths,args=json.loads(sys.argv[1]);sys.path[:0]=paths;"
        "sys.argv=['portfolio',*args];"
        "from tests.support.portfolio_engine import main;main()"
    )
    paths = [str(package_root()), str(metadata.distribution("rqalpha").locate_file(""))]
    command = [sys.executable, "-I", "-S", "-B", "-c", boot,
               json.dumps([paths, arguments])]
    with (output / (invocation + ".process.log")).open("x") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=90)
    (output / (invocation + ".process.json")).write_text(json.dumps({
        "command": command, "returncode": result.returncode,
        "fault": fault, "real_market_fixture": True,
        "scope": "mature account regression, not new ordinary product coverage",
    }, indent=2) + "\n")
    return result.returncode


@pytest.fixture(scope="module")
def longi(tmp_path_factory):
    value = os.getenv("ASHARE_QUANT_TEST_LONGI_CORPORATE")
    if not value:
        pytest.skip("Caller-supplied real Longi input/manifest/strategy required")
    source = Path(value)
    assert sha(source / "input.json") == INPUT_SHA256
    assert sha(source / "manifest.json") == MANIFEST_SHA256
    document = json.loads((source / "input.json").read_text())
    assert document["real_market_data"] is True
    assert document["securities"] == [SID]
    event, = document["corporate_schedule"]["rows"]
    assert (event["record_date"], event["payment_date"], event["share_credit_date"],
            event["new_shares_tradable_date"]) == (
                "2022-06-02", "2022-06-06", "2022-06-06", "2022-06-07")
    assert (event["cash_per_share"], event["capitalization_share_ratio"]) == ("0.26", "0.4")
    output = tmp_path_factory.mktemp("longi-uninterrupted")
    assert invoke(source, output, "baseline") == 0, (output / "baseline.process.log").read_text()
    receipt = json.loads((output / "result.json").read_text())
    entry, = receipt["corporate_entitlements"][SID].values()
    assert entry["basis"] == 1000 and entry["shares"] == 400
    assert entry["cash"] in ("260", "260.00")
    assert len(entry["stages"]) == 5
    # This archived rule starts with 1,000 shares and holds through the event.
    # Corporate credits must not fabricate trades; native-submit is covered by
    # the separately supplied trading portfolio fixtures.
    assert receipt["trades"] == []
    details = json.loads((output / "baseline.manifest.json").read_text())
    return source, output, receipt, details, event


def cursor(details, day, event):
    rows = [r for r in details["records"]
            if r["dt"].startswith(day + "T") and r["event"] == event]
    assert len(rows) == 1
    return rows[0]["cursor"]


@pytest.mark.parametrize("stage", FAULT_STAGES)
def test_real_longi_sigkill_resume_and_completed_reentry(longi, tmp_path, stage):
    source, baseline_dir, receipt, details, event = longi
    dates = {
        "corporate_record": (event["record_date"], "AFTER_TRADING"),
        "corporate_ex": (event["ex_date"], "BEFORE_TRADING"),
        "corporate_cash": (event["payment_date"], "BEFORE_TRADING"),
        "corporate_stock_credit": (event["share_credit_date"], "BEFORE_TRADING"),
        "corporate_stock_tradable": (event["new_shares_tradable_date"], "BEFORE_TRADING"),
        "decision_candidate": (details["records"][0]["dt"][:10], "BEFORE_TRADING"),
        "before_replace": (event["ex_date"], "BEFORE_TRADING"),
        "after_replace": (event["ex_date"], "BEFORE_TRADING"),
    }
    fault = f"{stage}:{cursor(details, *dates[stage])}"
    output = tmp_path / stage
    # A requested but unreached fault cannot pass this test.
    assert invoke(source, output, "interrupted", fault) == -signal.SIGKILL, (
        output / "interrupted.process.log").read_text()
    assert not (output / "result.json").exists()
    assert invoke(source, output, "resumed") == 0, (output / "resumed.process.log").read_text()
    assert (output / "result.json").read_bytes() == (baseline_dir / "result.json").read_bytes()
    recovered = json.loads((output / "result.json").read_text())
    assert len({trade["id"] for trade in recovered["trades"]}) == len(recovered["trades"])
    assert recovered["corporate_entitlements"] == receipt["corporate_entitlements"]
    checkpoint = sha(output / "native.chk")
    assert invoke(source, output, "completed-reentry") == 0
    assert sha(output / "native.chk") == checkpoint
    assert (output / "result.json").read_bytes() == (baseline_dir / "result.json").read_bytes()
    assert sha(source / "input.json") == INPUT_SHA256
    assert sha(source / "manifest.json") == MANIFEST_SHA256
