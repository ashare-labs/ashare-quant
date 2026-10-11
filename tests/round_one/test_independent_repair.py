"""Synthetic fixtures validate declared semantics; no market values used as answers."""

from copy import deepcopy

import pytest

from ashare_quant.round_one.independent_repair import (
    COUNT_MODEL,
    STANDARDIZATION,
    capture_window,
    count_projection,
    halt_attribution,
    standardized_projection,
)
from ashare_quant.round_one.repair_worker import denied, read


def product():
    return dict(
        product_id="C02-standardized",
        security="600009.XSHG",
        binding_sha256="a" * 64,
        role="independent_retrospective_query_research",
        historical_pit=False,
        strategy_visible=False,
        matching_feed=False,
        start_date="2023-02-02",
        end_date="2023-02-02",
        source=dict(url="synthetic://source", sha256="b" * 64),
        standardization_policy=STANDARDIZATION,
        rows=[
            dict(
                date="2023-02-02",
                fields=dict(
                    pre_close=dict(
                        state="value",
                        value="22.3",
                        origin="derived_previous_session_raw_close",
                        previous_date="2023-02-01",
                        exchange_ex_reference=False,
                    ),
                    factor=dict(
                        state="value",
                        value="1",
                        origin="declared_raw_price_identity",
                        corporate_action_factor=False,
                    ),
                    close=dict(state="value", value="23.1", origin="source"),
                    paused=dict(state="value", value=False, origin="derived"),
                ),
            )
        ],
    )


def op():
    return dict(
        api="get_price",
        parameters=dict(
            security="600009.XSHG",
            end_date="2023-02-02",
            frequency="daily",
            count=1,
            fq=None,
            skip_paused=False,
            fill_paused=False,
            fields=["pre_close", "factor", "paused"],
        ),
        as_of="2023-02-03T15:30:00+08:00",
    )


def test_standardization_requires_named_policy_and_preserves_non_ex_reference_meaning():
    raw = product()
    before = deepcopy(raw)
    value, provenance = standardized_projection(raw, op(), standardization=STANDARDIZATION)
    assert value.rows == [dict(date="2023-02-02", pre_close="22.3", factor="1", paused="0")]
    assert raw == before
    assert provenance["owner_field_capabilities"][0]["exchange_ex_reference"] is False
    assert provenance["owner_field_capabilities"][1]["corporate_action_factor"] is False
    with pytest.raises(ValueError, match="STANDARDIZATION_REQUIRED"):
        standardized_projection(raw, op(), standardization=None)


@pytest.mark.parametrize(
    "field,patch",
    [
        ("pre_close", dict(exchange_ex_reference=True)),
        ("pre_close", dict(previous_date="2023-02-02")),
        ("pre_close", dict(previous_date=None)),
        ("factor", dict(corporate_action_factor=True)),
        ("factor", dict(value="1.2")),
    ],
)
def test_incompatible_field_meanings_refused(field, patch):
    raw = product()
    raw["rows"][0]["fields"][field].update(patch)
    with pytest.raises(ValueError, match="MEANING_CHANGED"):
        standardized_projection(raw, op(), standardization=STANDARDIZATION)


def count_fixture():
    raw = product()
    raw["product_id"] = "C05-single-capture"
    raw["start_date"] = "2023-01-31"
    first = deepcopy(raw["rows"][0])
    first["date"] = "2023-01-31"
    raw["rows"].insert(0, first)
    operation = op()
    operation["parameters"].update(count=2, fields=["close", "paused"])
    return raw, operation


def test_observed_count_preserves_original_request_and_does_not_certify_gap():
    raw, operation = count_fixture()
    before = deepcopy(raw)
    value, provenance = count_projection(raw, operation, model=COUNT_MODEL)
    assert value.request.parameters == operation["parameters"]
    assert [r["date"] for r in value.rows] == ["2023-01-31", "2023-02-02"]
    assert provenance["full_session_coverage_certified"] is False
    assert provenance["actual_capture_versions"] == 1
    assert raw == before
    with pytest.raises(ValueError, match="MODEL_REQUIRED"):
        count_projection(raw, operation, model=None)


@pytest.mark.parametrize("change", ["wrong_count", "duplicate", "reverse", "different_end", "fill"])
def test_observed_count_scope_refusals(change):
    raw, operation = count_fixture()
    if change == "wrong_count":
        operation["parameters"]["count"] = 3
    if change == "duplicate":
        raw["rows"][1]["date"] = raw["rows"][0]["date"]
    if change == "reverse":
        raw["rows"].reverse()
    if change == "different_end":
        operation["parameters"]["end_date"] = "2023-02-03"
    if change == "fill":
        operation["parameters"]["fill_paused"] = True
    with pytest.raises(ValueError):
        count_projection(raw, operation, model=COUNT_MODEL)


def halt_fixture():
    history = product()
    halts = deepcopy(history)
    halts["rows"] = [
        dict(
            date="2023-02-01",
            full_day_paused=True,
            source_content="provider full-day record",
            official_announcement_chain_verified=False,
        )
    ]
    return history, halts


def test_halt_attribution_keeps_absent_bar_and_partial_evidence_limits():
    history, halts = halt_fixture()
    before = deepcopy(history)
    report = halt_attribution(history, halts, before, ["2023-02-01"])
    assert report["inserted_bars"] == 0 and report["daily_rows_unchanged"] is True
    assert report["official_notice_chain_certified"] is False
    assert history == before


@pytest.mark.parametrize(
    "change", ["history_changed", "missing_day", "partial_halt", "strategy_visible"]
)
def test_halt_attribution_refuses_unjustified_reclassification(change):
    history, halts = halt_fixture()
    prior = deepcopy(history)
    if change == "history_changed":
        history["rows"][0]["fields"]["close"]["value"] = "23.2"
    if change == "missing_day":
        halts["rows"][0]["date"] = "2023-01-31"
    if change == "partial_halt":
        halts["rows"][0]["full_day_paused"] = False
    if change == "strategy_visible":
        halts["strategy_visible"] = True
    with pytest.raises(ValueError):
        halt_attribution(history, halts, prior, ["2023-02-01"])


def test_bad_owner_pin_fails_before_loading_owner_or_network(tmp_path):
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    with pytest.raises(ValueError, match="PIN_CHANGED"):
        read(binding, tmp_path / "wheel.whl")
    with pytest.raises(RuntimeError, match="NETWORK_DENIED"):
        denied("example.com", 443)


def test_capture_window_is_actual_wall_time_not_logical_or_publication_time():
    events = [
        dict(
            kind="capture_start",
            seq=3,
            run_tag="one",
            observed_wall_utc="2026-10-10T14:33:10+00:00",
        ),
        dict(
            kind="capture_end", seq=6, run_tag="one", observed_wall_utc="2026-10-10T14:33:11+00:00"
        ),
    ]
    window = capture_window(events, 4)
    assert window["end"] == "2026-10-10T14:33:11+00:00"
    assert capture_window(events, 8) is None
    events[1]["observed_wall_utc"] = "2026-10-10T14:33:09+00:00"
    with pytest.raises(ValueError, match="WINDOW_INVALID"):
        capture_window(events, 4)
