"""Synthetic evidence verifies boundaries; no fixed real answers feed the mapper."""

from copy import deepcopy

import pytest

from ashare_quant.round_one.action_archives import (
    ARCHIVE_MODEL,
    action_receipt,
    archive_projection,
    archive_version_pair,
)
from ashare_quant.round_one.archive_worker import ROLE, denied, read


def archive(kind="daily", commit="first"):
    p = dict(
        product_id="C05-archive-" + kind,
        security="600123.XSHG",
        binding_sha256="a" * 64,
        role=ROLE,
        historical_pit=False,
        strategy_visible=False,
        matching_feed=False,
        source=dict(
            url="synthetic://" + commit,
            sha256="b" * 64,
            archive_commit=commit,
            repository_owner="synthetic",
            repository_name="sample",
            captured_end="2025-01-08T10:00:00+08:00",
        ),
        archive_version=dict(
            commit=commit,
            commit_time_raw="2025-01-07 01:00:00",
            commit_time_timezone=None,
            original_source_capture_time=None,
            source_publication_time=None,
            source_revision_id=None,
        ),
    )
    contract = dict(
        kind="archive_" + kind,
        security=p["security"],
        start_date="2025-01-06",
        end_date="2025-01-07",
        expected_dates=["2025-01-06", "2025-01-07"],
    )
    if kind == "daily":
        p.update(
            rows=[
                dict(
                    date=day,
                    fields=dict(
                        close=dict(state="value", value="15.21", origin="source"),
                        paused=dict(state="value", value=False, origin="derived"),
                    ),
                    native_fields=dict(close="15.21"),
                )
                for day in contract["expected_dates"]
            ]
        )
        op = dict(
            api="get_price",
            parameters=dict(
                security=p["security"],
                count=2,
                end_date="2025-01-07",
                frequency="daily",
                fields=["close", "paused"],
                fq=None,
                skip_paused=False,
                fill_paused=False,
            ),
            as_of="2025-01-07T15:30:00+08:00",
        )
    else:
        p.update(
            calendar_exchange="SSE",
            official_calendar_certified=False,
            rows=[
                dict(date="2025-01-06", exchange="SSE", is_open=False),
                dict(date="2025-01-07", exchange="SSE", is_open=True),
            ],
        )
        contract["exchange"] = "SSE"
        op = dict(
            api="get_trade_days",
            parameters=dict(start_date="2025-01-06", end_date="2025-01-07"),
            as_of="2025-01-07T15:30:00+08:00",
        )
    return p, contract, op


def project(p, c, o):
    return archive_projection(p, c, o, model=ARCHIVE_MODEL, calendar_exchange="SSE")


def test_daily_count_request_native_values_and_unknown_archive_clock_are_preserved():
    p, c, o = archive()
    before = deepcopy(p)
    value, provenance = project(p, c, o)
    assert p == before
    assert value.request.parameters == o["parameters"]
    assert value.rows[0] == dict(date="2025-01-06", close="15.21", paused="0")
    assert value.revision_id is None and value.published_at is None
    assert provenance["archive_version"]["commit_time_timezone"] is None
    assert provenance["upstream_capture_time_inferred"] is False
    assert provenance["native_fields_preserved"] == [dict(close="15.21")] * 2


def test_calendar_projects_only_open_labels_without_other_exchange_certification():
    p, c, o = archive("calendar")
    value, provenance = project(p, c, o)
    assert value.rows == [dict(date="2025-01-07")]
    assert provenance["other_exchange_inference"] is False
    assert provenance["official_calendar_certified"] is False
    with pytest.raises(ValueError, match="CALENDAR_SCOPE"):
        archive_projection(p, c, o, model=ARCHIVE_MODEL)
    with pytest.raises(ValueError, match="CALENDAR_SCOPE"):
        archive_projection(p, c, o, model=ARCHIVE_MODEL, calendar_exchange="SZSE")


@pytest.mark.parametrize(
    "change", ["count", "date", "duplicate", "security", "pit", "commit", "fill"]
)
def test_daily_scope_and_research_guards(change):
    p, c, o = archive()
    if change == "count":
        o["parameters"]["count"] = 1
    if change == "date":
        o["parameters"]["end_date"] = "2025-01-08"
    if change == "duplicate":
        p["rows"][1]["date"] = "2025-01-06"
    if change == "security":
        o["parameters"]["security"] = "000001.XSHE"
    if change == "pit":
        p["historical_pit"] = True
    if change == "commit":
        p["source"]["archive_commit"] = "other"
    if change == "fill":
        o["parameters"]["fill_paused"] = True
    with pytest.raises(ValueError):
        project(p, c, o)


def test_explicit_model_required():
    p, c, o = archive()
    with pytest.raises(ValueError, match="EXPLICIT_MODEL"):
        archive_projection(p, c, o, model=None)


def test_two_versions_with_equal_values_do_not_create_upstream_capture_or_revision_claims():
    a, c, o = archive()
    b, _, _ = archive(commit="second")
    result = archive_version_pair(
        a, b, [project(a, c, o)[0], project(b, c, o)[0]], fields=["close", "paused"]
    )
    assert result["selected_rows_identical"] is True
    assert result["original_capture_count"] is None
    assert result["source_value_revision_proven"] is False
    assert result["comparison"]["evidence_class"] == "SHARED_SOURCE_NOT_INDEPENDENT"
    assert result["comparison"]["case_verdict"] is None


def test_changed_archive_values_are_reported_without_inventing_upstream_revision():
    a, c, o = archive()
    b, _, _ = archive(commit="second")
    b["rows"][0]["fields"]["close"]["value"] = "15.22"
    result = archive_version_pair(
        a, b, [project(a, c, o)[0], project(b, c, o)[0]], fields=["close", "paused"]
    )
    assert result["selected_rows_identical"] is False
    assert result["comparison"]["comparison"]["differences"][0]["numeric_delta"] == "0.01"
    assert result["source_value_revision_proven"] is False
    with pytest.raises(ValueError, match="DISTINCT_COMMITS"):
        archive_version_pair(a, a, [project(a, c, o)[0]] * 2, fields=["close"])


def test_action_null_dates_and_special_rule_survive_without_factor_or_account_events():
    p, c, _ = archive()
    p.update(
        product_id="B25-issuer-evidence",
        historical_action_chain_complete=False,
        adjustment_factor_certified=False,
        documents={"notice": dict(path="notice.pdf", sha256="c" * 64)},
        rows=[
            dict(
                event_id="synthetic-event",
                ex_date=None,
                facts=dict(special_rule=True, price=None),
                citations=[dict(document="notice", pages=[2])],
            )
        ],
    )
    c.update(event_ids=["synthetic-event"])
    before = deepcopy(p)
    result = action_receipt(p, c)
    assert p == before and result["facts"][0]["ex_date"] is None
    assert result["adjustment_formula_inferred"] is False and result["engine_events_emitted"] == 0
    p["rows"][0]["citations"][0]["pages"] = [0]
    with pytest.raises(ValueError, match="CITATION_REQUIRED"):
        action_receipt(p, c)
    p = before
    p["adjustment_factor_certified"] = True
    with pytest.raises(ValueError, match="CAPABILITY_CHANGED"):
        action_receipt(p, c)


def test_owner_pin_and_network_guards(tmp_path):
    p = tmp_path / "binding.json"
    p.write_text("{}")
    with pytest.raises(ValueError, match="PIN_CHANGED"):
        read(p, tmp_path / "unused.whl")
    with pytest.raises(RuntimeError, match="NETWORK_DENIED"):
        denied("example.com", 443)
