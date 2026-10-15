"""Read-only external-corpus regression against an explicitly supplied public AD.

No source material is included here. These tests only reopen supplied packets;
they never collect data, run an account, or weaken production evidence pins.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.intraday import IntradayReader


@pytest.fixture(params=["plan", "phase", "intraday"])
def external_read(request, tmp_path):
    source = os.environ.get("ASHARE_CALENDAR_AD_SOURCE")
    python = os.environ.get("ASHARE_CALENDAR_AD_PYTHON")
    evidence = os.environ.get("ASHARE_PILOT_EVIDENCE")
    if not all((source, python, evidence)):
        pytest.skip("Explicit public AD source, Python and external evidence required")
    packet = None
    if request.param != "plan":
        path = os.environ.get("ASHARE_EXTERNAL_" + request.param.upper() + "_PACKET")
        if not path:
            pytest.skip("Explicit external packet required for this owner reader")
        packet = Path(path)
    store = packet.parent.parent if packet else tmp_path
    reader = IntradayReader(python, Path(source), tmp_path / "daily", store)
    config = SimpleNamespace(admission_reader_sha256=reader.pin())

    def invoke():
        if request.param == "plan":
            return reader.reviewed_plan()
        if request.param == "intraday":
            return reader.intraday(config, packet.stem)
        return reader.read(config, "phase", packet.stem)

    if packet:
        expected = json.loads(packet.read_bytes())
    else:
        expected = json.loads((Path(evidence) / "replan-20261014-15/manifest.json").read_bytes())
    return invoke, expected, request.param


def test_isolated_owner_reopens_explicit_external_evidence(external_read):
    invoke, expected, kind = external_read
    actual = invoke()
    if kind == "plan":
        assert actual["first_window"] == [expected["from_day"], expected["through_day"]]
        assert actual["scope"] == [code + ".XSHG" for code in expected["securities"]]
    else:
        assert actual == expected


@pytest.mark.parametrize(
    "damage,message",
    [
        ("unset", "须显式设置 ASHARE_PILOT_EVIDENCE"),
        ("missing", "缺少本地审阅基线清单"),
        ("modified", "审阅基线摘要不符"),
    ],
)
def test_missing_or_modified_external_evidence_stays_blocked(
    external_read, tmp_path, monkeypatch, damage, message
):
    invoke, _, _ = external_read
    if damage == "unset":
        monkeypatch.delenv("ASHARE_PILOT_EVIDENCE")
    else:
        root = tmp_path / "invalid-external-evidence"
        if damage == "modified":
            (root / "replan-20261014-15").mkdir(parents=True)
            (root / "manifest.json").write_text("{}")
            (root / "replan-20261014-15/manifest.json").write_text("{}")
        monkeypatch.setenv("ASHARE_PILOT_EVIDENCE", str(root))
    with pytest.raises(DomainError, match=message) as rejected:
        invoke()
    assert rejected.value.code == "PAPER_OWNER_READ_REJECTED"
