import json

import pytest

from ashare_quant.minute.inputs import MinuteInputs


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda d: d["minute"].pop(), "MINUTE_BAR_COVERAGE_GAP"),
        (lambda d: d["minute"].append(d["minute"][0]), "MINUTE_DUPLICATE_BAR"),
        (
            lambda d: d["minute"][0].update(available_at="2024-03-04T09:30:00+08:00"),
            "MINUTE_UNFINISHED_BAR_VISIBLE",
        ),
        (lambda d: d["minute"][0].update(high="1"), "MINUTE_INVALID_OHLC"),
        (lambda d: d["minute"][0].update(volume="1.5"), "MINUTE_VOLUME_NOT_SHARES"),
        (lambda d: d["states"].pop(), "MINUTE_STATE_COVERAGE_GAP"),
        (lambda d: d["states"][0].update(suspended=None), "MINUTE_EXPLICIT_STATE_REQUIRED"),
        (
            lambda d: d["states"][0].update(available_at="2024-03-04T10:00:00+08:00"),
            "MINUTE_STATE_NOT_AVAILABLE",
        ),
        (
            lambda d: d.update(actions=[{"kind": "cash_dividend"}]),
            "MINUTE_CORPORATE_ACTIONS_UNSUPPORTED",
        ),
        (lambda d: d["action_coverage"].update(status="unknown"), "MINUTE_ACTION_COVERAGE_UNKNOWN"),
        (lambda d: d.update(real_market_data=True), "MINUTE_REAL_INPUT_REQUIRES_AD"),
        (
            lambda d: d.update(visibility="assumed_closed"),
            "MINUTE_ASSUMED_VISIBILITY_REQUIRES_OPT_IN",
        ),
        (lambda d: d["calendar"].pop(), "MINUTE_PREVIOUS_SUCCESSOR"),
        (
            lambda d: d["sessions"][d["days"][0]].append(
                ["2024-03-04T12:00:00+08:00", "2024-03-04T12:01:00+08:00"]
            ),
            "MINUTE_SESSION_ORDER",
        ),
    ],
)
def test_malformed_incomplete_or_unproven_input_fails(minute_doc, tmp_path, mutation, error):
    mutation(minute_doc)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(minute_doc))
    with pytest.raises(ValueError, match=error):
        MinuteInputs(path)
