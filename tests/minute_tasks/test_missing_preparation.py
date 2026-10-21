"""Constructed protocol tests for the formal-query -> research gap adapter."""

from copy import deepcopy
from datetime import datetime

import pytest
from ashare_data import export_minute_research

from ashare_quant.backtests.minute.research import digest, validate_gaps
from ashare_quant.contracts import DomainError
from ashare_quant.tasks.minute.snapshot import IndexedQueries

SID = "600062.XSHG"
DAY = "2014-12-22"
FIELDS = ("open", "high", "low", "close", "volume", "amount")


def fixture():
    source = dict(
        id="constructed",
        kind="local_reconstruction",
        label="end",
        observed_at="2026-10-21T00:00:00+00:00",
        source_sha256="a" * 64,
    )
    rows = [
        dict(
            symbol=SID,
            timestamp=DAY + f"T09:{minute}:00+08:00",
            open=10,
            high=11,
            low=9,
            close=10,
            volume=100,
            amount=1000,
            quality="unverified",
        )
        for minute in (31, 33)
    ]
    quote = dict(
        security=SID,
        source_label=DAY + "T09:32:00+08:00",
        price_status="invalid",
        missing_reason="confirmed_invalid_price",
        missing_fields=list(FIELDS),
        **dict.fromkeys(FIELDS),
        diagnostics=["ohlc_envelope_error"],
        missing_evidence=[dict(source_file_sha256="b" * 64, source_row=7)],
        use_decision=dict(execution="deny", fresh_valuation="deny", reasons=["price_invalid"]),
    )
    bundle = dict(source=source, bars=rows, unavailable=[quote])
    metadata = dict(
        real_market_data=True,
        securities=[SID],
        days=[DAY],
        sessions={DAY: [[DAY + "T09:30:00+08:00", DAY + "T09:33:00+08:00"]]},
        gap_policy="explicit_unavailable",
    )
    return bundle, metadata


def test_confirmed_missing_survives_owner_export_and_consumer_binding():
    bundle, metadata = fixture()
    doc = export_minute_research(IndexedQueries({"fixture": True}, [bundle]), metadata)
    assert len(doc["minute"]) == 2 and len(doc["unavailable_minutes"]) == 1
    gap = doc["unavailable_minutes"][0]
    assert gap["reason"] == "confirmed_invalid_price"
    assert gap["source_label"] == DAY + "T09:32:00+08:00"
    assert (
        gap["evidence"]["coverage"]["unavailable_quotes"][0]["missing_evidence"][0]["source_row"]
        == 7
    )
    expected = {(SID, datetime.fromisoformat(DAY + f"T09:{n}:00+08:00")) for n in (31, 32, 33)}
    present = expected - {(SID, datetime.fromisoformat(gap["source_label"]))}
    assert len(validate_gaps(doc, expected, present)) == 1
    forged = deepcopy(doc)
    proof = forged["unavailable_minutes"][0]["evidence"]
    proof["coverage"]["unavailable_quotes"][0]["price_status"] = "conflict"
    proof["coverage_sha256"] = digest(proof["coverage"])
    with pytest.raises(ValueError, match="MINUTE_RESEARCH_GAP_BINDING"):
        validate_gaps(forged, expected, present)


def test_unknown_hole_is_not_promoted_to_confirmed_missing():
    bundle, metadata = fixture()
    bundle["unavailable"] = []
    with pytest.raises(DomainError) as exc:
        export_minute_research(IndexedQueries({"fixture": True}, [bundle]), metadata)
    assert exc.value.code == "PYTHON_MINUTE_GAPS"
    assert exc.value.details["missing"] == [{"symbol": SID, "bar_start": DAY + "T09:31:00+08:00"}]


def test_conflict_stays_a_restricted_observation_not_a_missing_quote():
    bundle, metadata = fixture()
    raw = bundle["bars"][0]
    raw["high"] = 2326.53
    raw["native_observation"] = dict(
        security=SID,
        source_label=raw["timestamp"],
        source_file_sha256="a" * 64,
        source_path="constructed",
        source_row=0,
        price_status="conflict",
        diagnostics=["existing_daily_reference_price_conflict"],
        **{k: raw[k] for k in FIELDS},
        use_decision=dict(execution="deny", fresh_valuation="deny", reasons=["price_conflict"]),
    )
    doc = export_minute_research(IndexedQueries({"fixture": True}, [bundle]), metadata)
    assert doc["minute"][0]["high"] == "2326.53"
    assert doc["minute"][0]["use_policy"]["reasons"] == ["price_conflict"]
    assert len(doc["unavailable_minutes"]) == 1
