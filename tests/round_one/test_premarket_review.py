import copy
import hashlib
import json

import pytest

from ashare_quant.round_one.premarket_review import MARK_FIELDS, read_supplement, review


@pytest.fixture
def observations():
    """Artificial prices: validates evidence handling, never represents a B26 run."""
    settings = dict(
        symbol="600000.XSHG", start_date="2023-01-03", end_date="2023-01-04",
        frequency="minute", initial_cash=100000, use_real_price=True, avoid_future_data=True,
        order_volume_ratio=1, all_fees=0, slippage=0, probe_times=["10:00"],
    )
    events = [dict(kind="identity", settings=settings, run_params=settings)]

    def number(value):
        return dict(value=value, float_hex=float(value).hex(), float17=format(float(value), ".17g"))

    for label, time in (("before_trading_start", "09:00:00"), ("09:01", "09:01:00")):
        at = "2023-01-04 " + time
        account = dict(
            kind="account_precision", time=at, label=label + ":before_reads",
            positions=[dict(security=settings["symbol"], fields={
                k: number(v) for k, v in dict(price=7.24, total_amount=100, value=724,
                                              avg_cost=7.3, hold_cost=7.241).items()
            })],
            portfolio={k: number(v) for k, v in dict(
                available_cash=99000, positions_value=724, total_value=99724,
            ).items()},
        )
        events.extend([
            dict(kind="probe_clock", label=label, time=at, previous_date="2023-01-03"),
            account,
            dict(kind="current_precision", label=label, time=at, symbol=settings["symbol"],
                 fields=dict(last_price=number(7.24))),
        ])
        for period, frequency in (("daily", "daily"), ("minute", "1m")):
            for fq in (None, "pre"):
                key = f"{label}:prior_{period}:{fq}"
                end = "2023-01-03" + (" 15:00:00" if period == "minute" else "")
                events.extend([
                    dict(kind="data_request", time=at, label=key, symbol=settings["symbol"],
                         parameters=dict(end_date=end, fq=fq, frequency=frequency)),
                    dict(kind="precise_data_result", time=at, label=key, rows=[
                        dict(index=end, fields=dict(close=number(7.891 if fq is None else 7.24))),
                    ]),
                ])
        events.append({**copy.deepcopy(account), "label": label + ":after_reads"})
    events.append(dict(kind="end", event_count_including_end=len(events) + 1))
    for seq, event in enumerate(events, 1):
        event["seq"] = seq
        event.setdefault("time", "2023-01-04 23:59:59")
    return events, settings


def load(tmp_path, events, settings):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return read_supplement(path, hashlib.sha256(path.read_bytes()).hexdigest(), settings)


def test_observed_reference_does_not_become_a_market_input_or_a_rounding_rule(tmp_path, observations):
    events, settings = observations
    result = load(tmp_path, events, settings)
    assert result["observations"][0]["public_mark"] == "7.24"
    assert result["observations"][0]["position"]["hold_cost"] == "7.241"
    assert not result["engine_input_eligible"]
    assert not result["internal_rounding_certified"]
    assert not result["independent_historical_pit_certified"]


@pytest.mark.parametrize("corruption", ["future", "late", "precision", "mutation", "settings", "duplicate"])
def test_invalid_or_later_evidence_is_refused(tmp_path, observations, corruption):
    events, settings = observations
    if corruption == "future":
        next(e for e in events if e["kind"] == "data_request")["parameters"]["end_date"] = "2023-01-04"
    elif corruption == "late":
        next(e for e in events if e["kind"] == "current_precision")["time"] = "2023-01-04 15:30:00"
    elif corruption == "precision":
        next(e for e in events if e["kind"] == "current_precision")["fields"]["last_price"]["float_hex"] = float(7.241).hex()
    elif corruption == "mutation":
        next(e for e in events if e.get("label") == "09:01:after_reads")["portfolio"]["available_cash"]["value"] = 99001
    elif corruption == "settings":
        settings = {**settings, "initial_cash": 1}
    else:
        events[2]["seq"] = events[1]["seq"]
    with pytest.raises(ValueError, match="PREMARKET_EVIDENCE"):
        load(tmp_path, events, settings)


def test_hash_is_checked_before_parsing(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("not JSON")
    with pytest.raises(ValueError, match="PREMARKET_EVIDENCE_HASH"):
        read_supplement(path, "0" * 64, {})


def test_review_preserves_all_values_and_differences(tmp_path, observations):
    evidence = load(tmp_path, *observations)
    rows = [dict(field=f, label="day_start", status="DIFFERENT", jq="7.24", aq="7.241",
                 numeric_delta="0.001", classification="UNPROVEN_JQ_EX_MARK_ROUNDING")
            for f in sorted(MARK_FIELDS)]
    rows.append(dict(field="/at", label="strategy_end", status="DIFFERENT",
                     classification="TERMINAL_CLOCK_OFFSET"))
    original = dict(comparisons=rows, differences=copy.deepcopy(rows), unresolved_count=0)
    before = copy.deepcopy(original)
    result = review(original, evidence)
    assert original == before
    assert result["material_mark_difference_count"] == 4
    assert len(result["differences"]) == 5
    assert result["mark_difference_resolved"] is False
    assert result["review_changes_engine_inputs"] is False
    for old, new in zip(rows, result["comparisons"]):
        for key in ("jq", "aq", "numeric_delta", "status"):
            assert old.get(key) == new.get(key)
    assert result["comparisons"][-1] == rows[-1]
    rows[0]["jq"] = "8"
    price = next(r for r in rows if r["field"] == "/positions/0/last_price")
    price["jq"] = "8"
    with pytest.raises(ValueError, match="REFERENCE_MISMATCH"):
        review(original, evidence)
