"""CMES partial facts are observations, never replacements for execution states.

The fact-provider seam below is synthetic contract evidence. No CMES file or
legacy feed is opened; installed-pair live acceptance uses the real AD reader.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ashare_quant.backtests.daily.python_api import DailyQuote
from ashare_quant.backtests.minute.api import Quote
from ashare_quant.contracts import DomainError
from ashare_quant.core.validation import stamp
from ashare_quant.tasks.minute import demand as demand_module
from ashare_quant.tasks.minute import prepare as prepare_module
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs

SID = "600000.XSHG"
DAYS = ["2026-10-20", "2026-10-21"]
PIN = {"root": "/synthetic-only", "release_id": 1, "manifest_sha256": "a" * 64}


class SyntheticFacts:
    def __init__(self):
        self.calls = []
        self.listed = "1999-11-10"
        self.listing_status = "source_observed"
        self.values = {DAYS[0]: False, DAYS[1]: True}

    def listing_date(self, security):
        self.calls.append(("listing_date", security))
        return {"security": security, "field": "source_reported_listing_date",
                "value": self.listed, "status": self.listing_status,
                "source": "cmes", "release": PIN, "source_refs": ["synthetic-contract"],
                "historical_available_at": None, "historical_pit": False}

    def st_status(self, security, day):
        self.calls.append(("st_status", security, day))
        value = self.values.get(day)
        return {"security": security, "date": day, "field": "is_st", "value": value,
                "status": "derived" if type(value) is bool else "unknown", "source": "cmes",
                "rule": "cmes-complete-history-membership.v1", "release": PIN,
                "source_refs": ["synthetic-contract"], "historical_available_at": None,
                "historical_pit": False, "snapshot": {"date": day, "rows": 2}}


@pytest.fixture
def partial_data(tmp_path, monkeypatch):
    provider = SyntheticFacts()
    release = SimpleNamespace(manifest={"cmes": {"synthetic_contract": True},
                                        "metadata": {"sha256": "b" * 64}},
                              identifiers={SID: 798}, instruments={SID: {"asset_class": "equity"}},
                              receipt=lambda: dict(PIN), verify=lambda descriptor: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Partial observations must not open prices or execution components")

    owner = SimpleNamespace(release=release, prices=forbidden)
    monkeypatch.setattr(demand_module, "ReleasedDailyProduct", lambda config: owner)
    monkeypatch.setattr(demand_module, "run_scope", lambda *a, **k: (DAYS, ["2026-10-19", *DAYS, "2026-10-22"]))
    monkeypatch.setattr(demand_module, "cmes_facts", lambda current: provider)
    monkeypatch.setattr(prepare_module, "cmes_facts", lambda current: provider)
    request = PythonCreate.model_validate(dict(
        environment_id="a" * 64, name="Synthetic partial facts", idempotency_key="partial-facts",
        source="from aquant import *\n", frequency="1m", warmup_sessions=20, securities=[],
        start_date=DAYS[0], end_date=DAYS[-1], initial_cash=100000,
        accept_research_model=True, trusted_local_code=True,
    ))
    data = DemandInputs(PIN, request, tmp_path / "private-task")
    try:
        yield data, provider
    finally:
        data.close()


@pytest.mark.parametrize("quote_type", [Quote, DailyQuote])
def test_dated_st_observation_needs_no_price_pause_or_execution_state(partial_data, monkeypatch, quote_type):
    data, provider = partial_data
    for name in ("component", "_row", "is_paused", "_corporate"):
        monkeypatch.setattr(data, name, lambda *a, **k: pytest.fail("unrelated prerequisite opened"))
    host = SimpleNamespace(data=data, day=DAYS[0], at=stamp(DAYS[0] + "T09:00:00+08:00"))
    assert quote_type(host, SID).is_st is False
    host.day, host.at = DAYS[1], stamp(DAYS[1] + "T09:00:00+08:00")
    assert quote_type(host, SID).is_st is True
    assert provider.calls == [("st_status", SID, day) for day in DAYS]
    assert list(data.reads) == []
    assert [row["fact"]["status"] for row in data.queries] == ["derived", "derived"]


def test_quote_preserves_0900_visibility_gate(partial_data):
    data, provider = partial_data
    host = SimpleNamespace(data=data, day=DAYS[0], at=stamp(DAYS[0] + "T08:59:00+08:00"))
    with pytest.raises(ValueError, match="PYTHON_DAILY_STATE_NOT_VISIBLE"):
        Quote(host, SID).is_st
    assert not provider.calls


def test_unknown_st_never_becomes_false_and_journal_keeps_proof(partial_data):
    data, provider = partial_data
    provider.values[DAYS[0]] = None
    with pytest.raises(DomainError) as error:
        data.is_st(SID, DAYS[0])
    assert error.value.code == "MINUTE_ST_STATUS_UNKNOWN"
    assert error.value.details["fact"]["value"] is None
    assert error.value.details["fact"]["status"] == "unknown"
    assert list(data.queries)[0]["fact"] == error.value.details["fact"]


def test_small_fact_cache_is_date_bound_and_not_caller_mutable(partial_data):
    data, provider = partial_data
    first = data.source_fact("st_status", SID, DAYS[0])
    first["value"] = True
    first["snapshot"]["date"] = "changed"
    assert data.is_st(SID, DAYS[0]) is False
    assert data.is_st(SID, DAYS[1]) is True
    assert len(provider.calls) == 2
    assert [r["cache_hit"] for r in data.queries] == [False, True, False]
    assert list(data.queries)[0]["fact"]["value"] is False


def test_fact_cache_still_refuses_changed_metadata_or_release_binding(partial_data, monkeypatch):
    from ashare_data.core.model import DataError

    data, provider = partial_data
    assert data.is_st(SID, DAYS[0]) is False
    def unavailable(descriptor):
        raise DataError("MARKET_OBJECT_INTEGRITY", "Selected metadata changed")
    monkeypatch.setattr(data.owner.release, "verify", unavailable)
    with pytest.raises(DomainError) as error:
        data.is_st(SID, DAYS[0])
    assert error.value.code == "MARKET_OBJECT_INTEGRITY"
    assert error.value.origin == "ashare-data"
    assert len(provider.calls) == 1 and len(list(data.queries)) == 1
    monkeypatch.setattr(data.owner.release, "verify", lambda descriptor: None)
    data._source_fact_cache["st_status", (SID, DAYS[0])]["release"] = {**PIN, "release_id": 2}
    with pytest.raises(DomainError) as error:
        data.is_st(SID, DAYS[0])
    assert error.value.code == "PYTHON_SOURCE_FACT_BINDING"


@pytest.mark.parametrize("replacement", [{"security": "000001.XSHE"}, {"date": DAYS[1]},
                                         {"field": "suspended"}, {"rule": "unreviewed.v1"}])
def test_partial_fact_query_identity_mismatch_is_not_cached(partial_data, monkeypatch, replacement):
    data, provider = partial_data
    original = provider.st_status
    monkeypatch.setattr(provider, "st_status", lambda sid, day: {**original(sid, day), **replacement})
    with pytest.raises(DomainError) as error:
        data.is_st(SID, DAYS[0])
    assert error.value.code == "PYTHON_SOURCE_FACT_BINDING"
    assert not data._source_fact_cache and not list(data.queries)


@pytest.mark.parametrize("field", ["tradestatus", "all_known"])
def test_trade_keeps_unknown_trading_or_listing_state_and_raw_nulls(partial_data, monkeypatch, field):
    data, _ = partial_data
    rows = [{"security": SID, "date": day, "close": "10.0", "tradestatus": "1",
             "is_st": None} for day in ("2026-10-19", DAYS[0])]
    if field == "tradestatus":
        rows[-1]["tradestatus"] = None
    original = deepcopy(rows)
    monkeypatch.setattr(data, "_daily", lambda *a, **k: rows)
    monkeypatch.setattr(data, "_corporate", lambda *a, **k: pytest.fail("corporate scan before known states"))
    with pytest.raises(DomainError) as error:
        data.component(SID, DAYS[0])
    if field == "tradestatus":
        assert error.value.code == "PYTHON_STATE_GAP"
        assert error.value.details["fields"] == ["tradestatus"]
        assert error.value.details["source_facts"]["is_st"]["value"] is False
    else:
        assert error.value.code == "PYTHON_LISTING_STATE_GAP"
        assert error.value.details["fields"] == ["historical_listing_status"]
    assert rows == original and not data.components and not data.corporate


@pytest.mark.parametrize("value,status", [(None, "unknown"), ("2026-10-21", "source_observed"),
                                        ("2026-10-01", "source_observed"), ("not-a-day", "source_observed")])
def test_listing_event_age_is_independent_of_observation_clock(partial_data, value, status):
    data, provider = partial_data
    provider.listed, provider.listing_status = value, status
    with pytest.raises(DomainError) as error:
        prepare_module.listing_facts(data.owner.release, [SID], facts=provider,
                                     fact_query=data.source_fact, on_date=DAYS[0])
    assert error.value.code in {"PYTHON_LISTING_GAP", "MINUTE_RESEARCH_RECENT_LISTING_UNSUPPORTED"}


def test_valid_listing_event_does_not_claim_active_interval(partial_data):
    data, provider = partial_data
    facts = prepare_module.listing_facts(data.owner.release, [SID], facts=provider,
                                        fact_query=data.source_fact, on_date=DAYS[0])
    assert facts[SID]["ipo"] == "1999-11-10"
    assert not ({"listed", "active", "out"} & facts[SID].keys())


def test_derived_membership_cannot_override_conflicting_raw_st(partial_data):
    data, provider = partial_data
    provider.values[DAYS[0]] = True
    rows = [{"security": SID, "date": DAYS[0], "tradestatus": None, "is_st": "0"}]
    original = deepcopy(rows)
    with pytest.raises(DomainError) as error:
        prepare_module.require_dated_states(rows, DAYS, facts=provider, fact_query=data.source_fact)
    assert error.value.code == "PYTHON_STATE_CONFLICT" and rows == original


def test_standalone_prepare_refuses_before_assembling_listed_true(partial_data, monkeypatch, tmp_path):
    data, _ = partial_data
    calendar = ["2026-10-19", *DAYS, "2026-10-22"]
    rows = [{"security": SID, "date": day, "close": "10.0", "tradestatus": "1", "is_st": None}
            for day in calendar[:-1]]
    original = deepcopy(rows)
    request = data.request.model_copy(update={"securities": (SID,)})
    monkeypatch.setattr(prepare_module, "scope", lambda *a, **k: (DAYS, calendar))
    monkeypatch.setattr(prepare_module, "ReleasedReferences", lambda *a, **k: pytest.fail("corporate opened"))
    with pytest.raises(DomainError) as error:
        prepare_module.prepare_market(PIN, request, owner=data.owner, daily={"rows": rows})
    assert error.value.code == "PYTHON_LISTING_STATE_GAP"
    assert rows == original and all("suspended" not in row for row in rows)


@pytest.mark.parametrize("raw,expected", [("0", False), ("1", True), (None, None)])
def test_non_cmes_st_uses_unchanged_raw_rule(partial_data, monkeypatch, raw, expected):
    data, _ = partial_data
    data.owner.release.manifest.clear()
    monkeypatch.setattr(data, "_row", lambda *a: {"is_st": raw})
    if expected is None:
        with pytest.raises(DomainError, match="unknown"):
            data.is_st(SID, DAYS[0])
    else:
        assert data.is_st(SID, DAYS[0]) is expected
    assert not list(data.queries)


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_native_observation_can_read_partial_st_with_flat_empty_universe(partial_data, monkeypatch, frequency):
    import rqalpha.main as rqmain
    import rqalpha.utils.config as rqconfig
    from rqalpha.const import INSTRUMENT_TYPE
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade
    from rqalpha.portfolio.position import POSITION_TYPE_MAP

    from ashare_quant.backtests.minute.native import run_native
    from ashare_quant.backtests.minute.runner import DEFAULTS

    data, _ = partial_data
    data.frequency, data.generic_research = frequency, frequency == "1m"
    settings = {**DEFAULTS, "lifecycle": "jq_minute"}
    if frequency == "1d":
        settings.update(matching="daily_open", lifecycle="daily", capacity_policy="daily")
    # run_native normally owns its fresh child. Preserve those process globals
    # here because this small native test intentionally runs inside pytest.
    for target, name in ((rqmain, "BaseDataSource"), (rqmain, "DataProxy"),
                         (rqconfig, "user_config"), (rqconfig, "project_config"),
                         (Order, "order_id_gen"), (Trade, "trade_id_gen")):
        monkeypatch.setattr(target, name, getattr(target, name))
    monkeypatch.setitem(POSITION_TYPE_MAP, INSTRUMENT_TYPE.CS, POSITION_TYPE_MAP[INSTRUMENT_TYPE.CS])
    source = b'''from aquant import *
def initialize(context):
    set_benchmark(None)
    run_daily(observe, time="09:50" if context.run_params.frequency == "minute" else "09:30")
def observe(context):
    observed = get_current_data()["600000.XSHG"].is_st
    expected = context.current_dt.date().isoformat() == "2026-10-21"
    assert observed is expected
    record(source_st=int(observed))
'''
    result = run_native(data.root, data, source, settings, "synthetic-partial-facts-native")
    assert result and len(list(data.queries)) == 2
    assert not data.components and list(data.reads) == []


def test_positive_trading_activity_and_listing_event_admit_execution_states(partial_data):
    data, provider = partial_data
    rows = [
        {
            "security": SID,
            "date": day,
            "open": "10.0",
            "high": "10.5",
            "low": "9.9",
            "close": "10.2",
            "preclose": "10.0",
            "volume": "10000",
            "amount": "102000.0",
            "tradestatus": None,
            "is_st": None,
        }
        for day in DAYS
    ]
    original = deepcopy(rows)
    listings = prepare_module.listing_facts(
        data.owner.release, [SID], facts=provider, fact_query=data.source_fact, on_date=DAYS[0]
    )
    resolved = prepare_module.require_dated_states(
        rows, DAYS, facts=provider, fact_query=data.source_fact
    )
    prepare_module.require_listing_window(
        data.owner.release, [SID], DAYS, listings=listings, rows=rows
    )
    assert resolved[SID, DAYS[0]] == {"tradestatus": "1", "is_st": "0"}
    assert resolved[SID, DAYS[1]] == {"tradestatus": "1", "is_st": "1"}
    assert rows == original

