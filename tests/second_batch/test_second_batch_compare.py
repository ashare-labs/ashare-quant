import json

import pytest
from ashare_quant.second_batch.compare import compare


def snapshot():
    return dict(
        time="2024-03-04 10:03:00",
        kind="snapshot",
        label="audit",
        portfolio=dict(available_cash=123, positions_value=456, total_value=579),
        positions=[
            dict(security="600000.XSHG", price=7.1, value=710),
            dict(security="000001.XSHE", price=10.1, value=1010),
        ],
        orders=[],
        trades=[
            dict(
                trade_id="t1",
                order_id="o1",
                security="600000.XSHG",
                time="2024-03-04 10:02:00",
                amount=100,
                price=7.1,
            )
        ],
        open_order_ids=[],
        observation_errors=[],
    )


def write(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_missing_mark_does_not_hide_cash_execution_or_other_security(tmp_path):
    a, b = snapshot(), snapshot()
    b["portfolio"]["available_cash"] = -3
    b["positions"][1]["price"] = 10.2
    b["trades"][0]["price"] = 7.2
    result = compare(
        write(tmp_path / "jq", [a]),
        write(tmp_path / "aq", [b]),
        gaps=[dict(at="2024-03-04T10:03:00+08:00", security="600000.XSHG", reason="missing")],
    )
    checks = {c["field"]: c for c in result["checks"]}
    assert checks["/portfolio/available_cash"]["status"] == "difference"
    assert checks["/trades/0/price"]["status"] == "difference"
    assert checks["/positions/0/price"]["status"] == "difference"
    assert checks["/positions/1/price"]["status"] == "unverified_missing_mark"
    assert checks["/portfolio/total_value"]["status"] == "unverified_missing_mark"
    assert result["summary"]["unverified_missing_mark"] == 4


def test_missing_event_is_a_difference_and_duplicate_key_fails(tmp_path):
    a = snapshot()
    b = dict(a, label="extra")
    result = compare(write(tmp_path / "jq", [a, b]), write(tmp_path / "aq", [a]))
    assert result["summary"]["difference"] == 1
    assert any(c["field"] == "/" and c["aq"] is None for c in result["checks"])
    with pytest.raises(ValueError, match="DUPLICATE_COMPARISON_KEY"):
        compare(write(tmp_path / "jq", [a, a]), tmp_path / "aq")


def test_empty_comparison_is_not_a_pass(tmp_path):
    empty = write(tmp_path / "empty", [])
    with pytest.raises(ValueError, match="EMPTY_INPUT"):
        compare(empty, empty)
