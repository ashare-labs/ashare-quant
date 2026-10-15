"""Real sealed responses through the collector, admission and native account.

Set ASHARE_RESPONSE_CORPUS and ASHARE_CALENDAR_AD_SOURCE explicitly. No live
evidence is collected and no existing account is passed to the entry point.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "scripts/replay_sealed_responses.py"


def inputs():
    corpus = os.environ.get("ASHARE_RESPONSE_CORPUS")
    owner = os.environ.get("ASHARE_CALENDAR_AD_SOURCE")
    if not corpus or not owner:
        pytest.skip("Explicit sealed response corpus and candidate owner source required")
    return Path(corpus), Path(owner)


def invoke(corpus, owner, output):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(ENTRY),
            "--corpus",
            str(corpus),
            "--ad-source",
            str(owner),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_real_responses_twice_have_identical_reports_and_keep_market_labels(tmp_path):
    corpus, owner = inputs()
    for name in ("first", "second"):
        result = invoke(corpus, owner, tmp_path / name)
        assert result.returncode == 0, result.stdout + result.stderr
    raw = (tmp_path / "first/report.json").read_bytes()
    assert raw == (tmp_path / "second/report.json").read_bytes()
    report = json.loads(raw)
    assert report["passed"] is True and report["market_result"] is False
    assert report["originals"]["15-execution-01"]["program_error"] == "TypeError"
    assert report["native"]["14-opening"]["cash"] == "13436"
    assert report["native"]["15-opening"]["trades"] == []
    assert report["native"]["15-opening"]["positions"] == report["native"]["15-opening"]["sellable"]
    assert report["native"]["derived_utc_and_crash_recovery"] == report["native"]["14-opening"]
    for name in ("first", "second"):
        account = json.loads((tmp_path / name / "account/state.json").read_bytes())
        assert account["config"]["account_id"].startswith("replay-only-")
        assert all(
            d["decision"]["evidence_kind"] == "sealed_response_replay"
            for d in account["days"].values()
        )


@pytest.mark.parametrize("damage", ["body", "receipt_clock"])
def test_changed_raw_or_envelope_is_rejected_before_account_creation(tmp_path, damage):
    source, owner = inputs()
    corpus = tmp_path / "tampered"
    shutil.copytree(source, corpus)
    if damage == "body":
        path = next((corpus / "blobs").iterdir())
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path = corpus / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["events"]["14-admission"]["completed_at"] = "2026-10-14T05:06:00Z"
        path.write_text(json.dumps(manifest))
    output = tmp_path / "rejected"
    result = invoke(corpus, owner, output)
    assert result.returncode != 0
    assert not (output / "account").exists()
    assert "DomainError" in result.stderr
