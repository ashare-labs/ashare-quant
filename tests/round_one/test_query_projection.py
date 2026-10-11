"""Synthetic evidence for table formatting, not a model of market activity."""

from copy import deepcopy

import pytest

from ashare_quant.round_one.query_projection import MODEL, project, sealed_products


def inputs(*, fill=False, skip=False):
    p = dict(
        product_id="synthetic-daily",
        security="000003.XSHE",
        binding_sha256="a" * 64,
        role="independent_retrospective_query_research",
        historical_pit=False,
        strategy_visible=False,
        matching_feed=False,
        start_date="2024-02-01",
        end_date="2024-02-01",
        source=dict(url="synthetic://owner", sha256="b" * 64),
        raw_row_present=False,
        pause_fact=dict(full_day_paused=True, security="000003.XSHE", date="2024-02-01"),
        rows=[
            dict(
                date="2024-02-01",
                fields=dict(
                    close=dict(state="nan", value=None, origin="missing_paused_row"),
                    volume=dict(state="nan", value=None, origin="missing_paused_row"),
                    paused=dict(state="value", value=True, origin="documented_fact"),
                ),
            )
        ],
    )
    op = dict(
        api="get_price",
        parameters=dict(
            security="000003.XSHE",
            frequency="daily",
            fields=["close", "volume", "paused"],
            start_date="2024-02-01",
            end_date="2024-02-01",
            fq=None,
            fill_paused=fill,
            skip_paused=skip,
        ),
        as_of="2024-02-01T15:30:00+08:00",
    )
    return p, op


def test_unfilled_missing_table_cells_are_nan_and_independent_fact_survives():
    p, op = inputs()
    before = deepcopy(p)
    result, meta = project(p, op, model=MODEL)
    assert p == before
    assert result.rows == [
        dict(
            date="2024-02-01",
            close={"nonfinite": "nan"},
            volume={"nonfinite": "nan"},
            paused={"nonfinite": "nan"},
        )
    ]
    assert meta["owner_pause_fact"]["full_day_paused"] is True
    assert meta["owner_rows_unchanged"] == p["rows"]
    assert meta["reference_answers_used"] is False
    assert [c["field"] for c in meta["changes"]] == ["paused"]


def test_filled_prices_pass_through_and_only_paused_column_type_changes():
    p, op = inputs(fill=True)
    p["rows"][0]["fields"]["close"] = dict(
        state="value", value="17.23", origin="synthetic_paused_fill"
    )
    p["rows"][0]["fields"]["volume"] = dict(
        state="value", value="0", origin="synthetic_paused_fill"
    )
    result, meta = project(p, op, model=MODEL)
    assert result.rows[0]["close"] == "17.23" and result.rows[0]["volume"] == "0"
    assert result.rows[0]["paused"] == "1"
    assert [c["rule"] for c in meta["changes"]] == ["numeric_paused_table_column"]


def test_skipped_empty_result_stays_empty():
    p, op = inputs(skip=True)
    p["rows"] = []
    result, meta = project(p, op, model=MODEL)
    assert result.rows == [] and meta["changes"] == []


def test_unknown_row_presence_cannot_be_inferred_from_nan_values():
    p, op = inputs()
    p["raw_row_present"] = None
    result, meta = project(p, op, model=MODEL)
    assert result.rows[0]["paused"] == "1"
    assert all(c["rule"] != "missing_unfilled_table_row" for c in meta["changes"])


@pytest.mark.parametrize(
    "fact",
    [
        {},
        dict(full_day_paused=False, security="000003.XSHE"),
        dict(full_day_paused=True, security="different"),
    ],
)
def test_mask_requires_independent_positive_pause_fact(fact):
    p, op = inputs()
    p["pause_fact"] = fact
    with pytest.raises(ValueError, match="MISSING_PAUSE_FACT"):
        project(p, op, model=MODEL)


def test_no_unsupported_field_is_disguised_as_nan():
    p, op = inputs()
    op["parameters"]["fields"].append("vwap")
    with pytest.raises(ValueError, match="CANNOT_MASK_UNSUPPORTED"):
        project(p, op, model=MODEL)


def test_false_status_as_numeric_zero_without_price_or_avg_rounding():
    p, op = inputs(fill=True)
    p["raw_row_present"] = True
    p["rows"][0]["fields"]["paused"] = dict(state="value", value=False, origin="derived")
    p["rows"][0]["fields"]["close"] = dict(state="value", value="10.123456", origin="source")
    result, meta = project(p, op, model=MODEL)
    assert result.rows[0]["paused"] == "0" and result.rows[0]["close"] == "10.123456"
    assert len(meta["changes"]) == 1


def test_model_and_owner_role_are_explicit():
    p, op = inputs()
    with pytest.raises(ValueError, match="MODEL_REQUIRED"):
        project(p, op, model="automatic")
    p["strategy_visible"] = True
    with pytest.raises(ValueError, match="CAPABILITY_CHANGED"):
        project(p, op, model=MODEL)


def test_replay_refuses_changed_base_seal(tmp_path):
    (tmp_path / "complete.json").write_text("{}")
    with pytest.raises(ValueError, match="BASE_SEAL_CHANGED"):
        sealed_products(tmp_path)
