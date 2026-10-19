"""Provider/date-neutral synthetic contracts, through the public AD exporter."""

import json
from copy import deepcopy
from decimal import Context, localcontext

import pytest
from ashare_data import Store, export_minute_research

from ashare_quant.backtests.minute import MinuteInputs, run_backtest
from ashare_quant.backtests.minute.research import ASSUMPTIONS, MILESTONES, PROFILE, digest

ACCEPT = dict(accept_assumed=True, accept_state_replay=True, accept_research=True)


def make_document(root, *, ids=("601234.XSHG", "000321.XSHE"), calendar=None, edit=None):
    calendar = calendar or ["2025-07-01", "2025-07-02", "2025-07-03", "2025-07-04"]
    days = calendar[1:-1]
    sessions = {d: [[d + "T09:30:00+08:00", d + "T09:34:00+08:00"]] for d in days}
    source = dict(
        id="arbitrary-provider-protocol-fixture",
        kind="synthetic",
        location="generated:test",
        observed_at=calendar[-1] + "T16:00:00+08:00",
        label="end",
        time_semantics="verified",
        time_evidence="Synthetic protocol construction",
        volume_unit="shares",
        amount_unit="CNY",
        price_unit="CNY",
        price_basis="unadjusted",
        unit_evidence="Synthetic units",
    )
    bundle = dict(
        schema_version=1,
        source=source,
        calendar=[
            dict(
                date=d,
                is_open=True,
                sessions=sessions[d],
                available_at=source["observed_at"],
                evidence="Synthetic calendar",
            )
            for d in days
        ],
        bars=[
            dict(
                symbol=s,
                timestamp=d + f"T09:{n}:00+08:00",
                open="10",
                high="10",
                low="10",
                close="10",
                volume="10000",
                amount="100000",
                quality="synthetic",
                quality_reason="engineering only",
                available_at=None,
                source_note="原文",
            )
            for d in days
            for s in ids
            for n in range(31, 35)
        ],
    )
    metadata = dict(
        real_market_data=False,
        securities=list(ids),
        calendar=calendar,
        days=days,
        sessions=sessions,
        price_basis="raw",
        volume_unit="share",
        amount_unit="CNY",
        visibility="assumed_closed",
        state_visibility="posthoc_daily_replay",
        limitations=["Synthetic test, not real evidence"],
        state_replay=dict(
            model="dated-daily-state-0900.v1",
            purpose="retrospective_research",
            evidence=["fixture"],
            limitations=["Unknown historical availability"],
        ),
        source_refs={
            "evidence": {"fixture": {"source": "synthetic engineering", "sha256": "a" * 64}}
        },
        research=dict(
            profile=PROFILE,
            purpose="retrospective_research",
            assumptions=ASSUMPTIONS,
            strict_execution_admitted=False,
            interval_verified=False,
            price_limit_verified=False,
            historical_pit=False,
            envelope="raw-previous-close-4pct-inward-cent.v1",
            capabilities=dict(
                matching="next_open",
                lifecycle="closed_bars",
                capacity_policy="strict_minute",
                initial_positions="flat",
                terminal_positions="flat",
            ),
            calendars={
                s.split(".")[1]: dict(days={d: True for d in calendar}, source_refs=["fixture"])
                for s in ids
            },
        ),
        states=[],
        actions=[],
        action_coverage=dict(
            status="bounded_sources_no_known_overlap",
            complete=False,
            absence_proved=False,
            historical_pit=False,
            start=calendar[0],
            end=days[-1],
            evidence=["fixture"],
            queries=[
                dict(
                    security=s,
                    query_complete=True,
                    start=calendar[0],
                    end=days[-1],
                    milestones_checked=MILESTONES,
                    source_rows=0,
                    events=[],
                    source_refs=["fixture"],
                    unknown_dates_are_not_no_event=True,
                )
                for s in ids
            ],
        ),
    )
    for s in ids:
        for d in days:
            prior = calendar[calendar.index(d) - 1]
            facts = dict(
                listed=True,
                suspended=False,
                is_st=False,
                previous_close="10",
                first_listing_date="2001-01-01",
            )
            metadata["states"].append(
                dict(
                    security=s,
                    date=d,
                    **facts,
                    reference_price=None,
                    upper_limit=None,
                    lower_limit=None,
                    available_at=None,
                    historical_available_at=None,
                    observed_at=source["observed_at"],
                    replay_effective_at=d + "T09:00:00+08:00",
                    evidence={"source_refs": ["fixture"]},
                    source_observation=dict(
                        security=s,
                        date=d,
                        historical_available_at=None,
                        intraday_halts_verified=False,
                        dependency_conflicts=[],
                        source_quality=dict(
                            execution_permission=False, limitations=["Synthetic fixture"]
                        ),
                        previous_session=prior,
                        **{
                            k: dict(value=v, status="source_observed", source_refs=["fixture"])
                            for k, v in facts.items()
                        },
                        **{
                            k: dict(value=None)
                            for k in ["reference_price", "upper_limit", "lower_limit"]
                        },
                    ),
                    previous_close_observation=dict(
                        security=s,
                        date=prior,
                        close="10",
                        price_basis="raw",
                        source_refs=["fixture"],
                    ),
                )
            )
    if edit:
        edit(bundle, metadata)
    with localcontext(Context(prec=28)):
        store = Store.init(root / "store")
        batch = store.import_bundle(bundle)
        requirements = [
            dict(symbols=list(ids), start=a, end=b)
            for intervals in sessions.values()
            for a, b in intervals
        ]
        if metadata.get("gap_policy") == "explicit_unavailable":
            from datetime import datetime, timedelta

            requirements = [
                dict(
                    symbols=[r["symbol"]],
                    start=(
                        datetime.fromisoformat(r["timestamp"]) - timedelta(minutes=1)
                    ).isoformat(),
                    end=r["timestamp"],
                )
                for r in bundle["bars"]
            ]
        report = store.validate(
            [batch],
            policy="synthetic",
            requirements=requirements,
        )
        assert report["passed"]
        snapshot = store.publish(report["id"])
        with store.snapshot(snapshot) as view:
            return export_minute_research(view, metadata)


def save(doc, path, *, rehash=True):
    if rehash:
        doc["content_sha256"] = digest({k: v for k, v in doc.items() if k != "content_sha256"})
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def doc(tmp_path):
    return make_document(tmp_path)


@pytest.mark.parametrize(
    "ids,calendar",
    [
        (("601234.XSHG", "000321.XSHE"), ["2025-07-01", "2025-07-02", "2025-07-03", "2025-07-04"]),
        (("600519.XSHG",), ["2019-04-08", "2019-04-09", "2019-04-10", "2019-04-11"]),
    ],
)
def test_arbitrary_bounded_windows_use_same_contract(tmp_path, ids, calendar):
    document = make_document(tmp_path, ids=ids, calendar=calendar)
    original = deepcopy(document)
    data = MinuteInputs(save(document, tmp_path / "input.json"), **ACCEPT)
    assert data.document == original
    assert len(data.bars) == 8 * len(ids)
    assert data.generic_research and not data.full_session_input
    assert data.preflight()["valuation_cutoff"][calendar[-2]].endswith("09:34:00+08:00")
    assert not data.preflight()["historical_pit_verified"]


@pytest.mark.parametrize("flag", ACCEPT)
def test_explicit_optins_are_independent(doc, tmp_path, flag):
    with pytest.raises(ValueError, match="REQUIRES_OPT_IN"):
        MinuteInputs(save(doc, tmp_path / "input.json"), **{**ACCEPT, flag: False})


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda d: d["minute"].pop(), "BAR_COVERAGE_GAP"),
        (lambda d: d["minute"].append(d["minute"][0]), "DUPLICATE_BAR"),
        (lambda d: d["states"].pop(), "STATE_COVERAGE_GAP"),
        (lambda d: d["states"][0].update(is_st=True), "STATE_PROJECTION"),
        (
            lambda d: d["states"][0]["previous_close_observation"].update(date="2025-06-30"),
            "PREVIOUS_CLOSE_MISMATCH",
        ),
        (lambda d: d["states"][0].update(upper_limit="11"), "UNKNOWN_LIMITS"),
        (lambda d: d["minute"][0].update(quality="observed"), "BAR_SOURCE_BINDING"),
        (lambda d: d["minute"][0].update(open="10.01"), "INVALID_OHLC"),
        (
            lambda d: d["minute"][0].update(available_at="2025-07-02T09:31:00+08:00"),
            "UNKNOWN_BAR_CLOCKS",
        ),
        (
            lambda d: d["action_coverage"]["queries"][0].update(query_complete=False),
            "INCOMPLETE_ACTION_QUERY",
        ),
        (lambda d: d["action_coverage"].update(absence_proved=True), "BOUNDED_ACTION_EVIDENCE"),
        (lambda d: d["source_refs"]["evidence"].clear(), "EVIDENCE_CATALOG"),
        (lambda d: d["research"].update(historical_pit=True), "PROFILE_REQUIRED"),
    ],
)
def test_bad_inputs_are_rejected_before_execution(doc, tmp_path, mutation, error):
    mutation(doc)
    with pytest.raises(ValueError, match=error):
        MinuteInputs(save(doc, tmp_path / "bad.json"), **ACCEPT)


def test_known_payment_overlap_and_source_tampering(doc, tmp_path):
    q = doc["action_coverage"]["queries"][0]
    q["events"] = [
        dict(
            security=q["security"],
            source_refs=["fixture"],
            **{k: "2025-07-02" if k == "payment_date" else None for k in MILESTONES},
        )
    ]
    q["source_rows"] = 1
    with pytest.raises(ValueError, match="KNOWN_ACTION_IN_WINDOW"):
        MinuteInputs(save(doc, tmp_path / "action.json"), **ACCEPT)
    q["events"], q["source_rows"] = [], 0
    save(doc, tmp_path / "unused.json")
    doc["limitations"].append("tampered")
    with pytest.raises(ValueError, match="CONTENT_IDENTITY"):
        MinuteInputs(save(doc, tmp_path / "hash.json", rehash=False), **ACCEPT)


def test_zero_activity_needs_explicit_bound_phase_evidence(doc, tmp_path):
    row = doc["minute"][0]
    row.update(volume="0", amount="0")
    row["evidence"]["ad_row"].update(volume="0", amount="0")
    row["evidence"]["row_sha256"] = digest(row["evidence"]["ad_row"])
    row["use_policy"]["binding"]["row_sha256"] = row["evidence"]["row_sha256"]
    with pytest.raises(ValueError, match="ZERO_VOLUME_PHASE_EVIDENCE"):
        MinuteInputs(save(doc, tmp_path / "zero.json"), **ACCEPT)


@pytest.mark.parametrize("field,value", [("kind", "unregistered"), ("price_basis", "adjusted")])
def test_rehashed_source_contract_still_must_match_capability(doc, tmp_path, field, value):
    source = doc["source_refs"]["ad_lineage"]["batches"][0]["source"]
    source[field] = value
    for row in doc["minute"]:
        row["evidence"]["source_contract_sha256"] = digest(source)
    with pytest.raises(ValueError, match="BAR_SOURCE_BINDING"):
        MinuteInputs(save(doc, tmp_path / "source.json"), **ACCEPT)


def test_recent_listing_is_not_an_ordinary_existing_stock(doc, tmp_path):
    state = doc["states"][0]
    state["first_listing_date"] = "2025-07-01"
    state["source_observation"]["first_listing_date"]["value"] = "2025-07-01"
    with pytest.raises(ValueError, match="RECENT_LISTING_UNSUPPORTED"):
        MinuteInputs(save(doc, tmp_path / "ipo.json"), **ACCEPT)


def test_unsupported_execution_policy_is_not_silently_accepted(doc, tmp_path):
    strategy = tmp_path / "strategy.py"
    strategy.write_text("def initialize(context): pass\n")
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_UNSUPPORTED"):
        run_backtest(
            strategy,
            save(doc, tmp_path / "input.json"),
            tmp_path / "refused",
            config={"matching": "jq_current"},
            **ACCEPT,
        )
    assert not (tmp_path / "refused").exists()


def test_generic_contract_runs_native_account_and_completed_reentry(doc, tmp_path):
    strategy = tmp_path / "strategy.py"
    strategy.write_text("""from jqdata import order, order_target, run_daily
def initialize(context): run_daily(trade, '09:31')
def trade(context):
    if str(context.current_dt.date()) == '2025-07-02': order('601234.XSHG', 100)
    else: order_target('601234.XSHG', 0)
""")
    path = save(doc, tmp_path / "input.json")
    first = run_backtest(strategy, path, tmp_path / "run", **ACCEPT)
    again = run_backtest(strategy, path, tmp_path / "run", **ACCEPT)
    assert first["native_run"] and not again["native_run"]
    assert first["result"] == again["result"]
    assert first["result"]["native_classes"]["account"] == "rqalpha.portfolio.account.Account"
