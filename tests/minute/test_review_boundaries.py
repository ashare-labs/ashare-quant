"""Economic and clock regressions for independent review F1/F2/F3."""

import copy
import json

import pytest

from ashare_quant.backtests.minute.inputs import MinuteInputs, stamp
from ashare_quant.backtests.minute.runner import MinuteRunError, run_backtest

ZERO = {"commission_rate": 0, "sell_tax": 0, "min_commission": 0}
SID = "600000.XSHG"


def flat_window(doc, days=1):
    """Keep real native execution, but give the engineered valuation test a flat baseline."""
    doc = copy.deepcopy(doc)
    doc["days"] = doc["days"][:days]
    doc["sessions"] = {d: doc["sessions"][d] for d in doc["days"]}
    doc["states"] = [s for s in doc["states"] if s["date"] in doc["days"]]
    doc["minute"] = [r for r in doc["minute"] if r["bar_end"][:10] in doc["days"]]
    for s in doc["states"]:
        s["previous_close"] = "10"
    for r in doc["minute"]:
        r.update(open="10", high="10", low="10", close="10", amount=str(int(r["volume"]) * 10))
    return doc


@pytest.mark.parametrize(
    "available,expected", [("15:01:00", 11), ("15:30:00", 11), ("15:30:01", 10)]
)
def test_after_close_native_mark_uses_only_visible_prices_without_late_matching(
    run_minute, minute_doc, available, expected
):
    doc = flat_window(minute_doc)
    for r in doc["minute"]:
        if r["security"] == SID and "T15:00:" in r["bar_end"]:
            r.update(close="11", high="11", available_at=f"2024-03-04T{available}+08:00")
    result, events, _ = run_minute(
        """from jqdata import *
def initialize(c):
    run_daily(buy, 'open')
    run_daily(pending, '14:59')
def buy(c): order('600000.XSHG', 100)
def pending(c): order('600000.XSHG', 100, LimitOrderStyle(11))
def after_trading_end(c):
    p = c.portfolio
    record(quote=get_current_data()['600000.XSHG'].last_price,
           mark=p.positions['600000.XSHG'].price, equity=p.total_value)
""",
        doc=doc,
        config=ZERO,
    )
    expected_equity = 99000 + 100 * expected
    values = [e["values"] for e in events if e["kind"] == "record"]
    assert values == [{"quote": expected, "mark": expected, "equity": expected_equity}]
    assert result["accounts"][0]["total_value"] == expected_equity
    assert result["final_account"]["total_value"] == expected_equity
    assert len(result["trades"]) == 1
    assert result["orders"][1]["status"] == "rejected"
    assert result["orders"][1]["filled"] == 0


@pytest.mark.parametrize("available,history_minute", [("08:59", 0), ("09:01", 59)])
def test_next_day_preopen_marks_dated_reference_without_revealing_late_bar(
    run_minute, minute_doc, available, history_minute
):
    doc = flat_window(minute_doc, days=2)
    for r in doc["minute"]:
        if r["security"] == SID and r["bar_end"] == "2024-03-04T15:00:00+08:00":
            r.update(close="11", high="11", available_at=f"2024-03-05T{available}:00+08:00")
    for s in doc["states"]:
        if s["security"] == SID and s["date"] == "2024-03-05":
            s["previous_close"] = "11"  # Distinct, declared pre-open reference evidence.
    result, events, _ = run_minute(
        f"""from jqdata import *
def initialize(c): run_daily(buy, 'open')
def buy(c):
    if str(c.current_dt.date()) == '2024-03-04': order('600000.XSHG', 100)
def before_trading_start(c):
    if str(c.current_dt.date()) == '2024-03-05':
        p = c.portfolio
        h = get_price('600000.XSHG', count=1, frequency='1m', fq=None,
                      fields=['close'], end_date='2024-03-04T15:00:00+08:00')
        assert h.index[-1].minute == {history_minute}
        assert p.positions['600000.XSHG'].closeable_amount == 100
        record(quote=get_current_data()['600000.XSHG'].last_price,
               mark=p.positions['600000.XSHG'].price, equity=p.total_value)
""",
        doc=doc,
        config=ZERO,
    )
    assert result["accounts"][0]["total_value"] == 100000
    assert [e["values"] for e in events if e["kind"] == "record"] == [
        {"quote": 11, "mark": 11, "equity": 100100}
    ]


def test_history_endpoint_and_knowledge_clock_stay_independent(run_minute, minute_doc):
    doc = flat_window(minute_doc)
    for r in doc["minute"]:
        if r["security"] == SID and "T09:31:" in r["bar_end"]:
            r["available_at"] = "2024-03-04T09:34:00+08:00"
    _, events, _ = run_minute(
        """from jqdata import *
def initialize(c):
    run_daily(early, '09:33')
    run_daily(later, '09:34')
def early(c):
    for query in (lambda: get_price('600000.XSHG', count=1, frequency='1m', fq=None,
                                   end_date=c.current_dt.replace(minute=31)),
                  lambda: get_bars('600000.XSHG', 1, '1m', end_dt=c.current_dt.replace(minute=31))):
        try: query()
        except ValueError as e: assert 'INSUFFICIENT_VISIBLE_HISTORY' in str(e)
        else: raise AssertionError('unavailable bar exposed')
def later(c):
    p = get_price('600000.XSHG', count=1, frequency='1m', fq=None,
                  end_date=c.current_dt.replace(minute=31))
    assert p.index[-1].minute == 31
    for include in (False, True):
        b = get_bars('600000.XSHG', 1, '1m', ['date','close'], include_now=include,
                     end_dt=c.current_dt.replace(minute=31), df=True)
        assert b.date.iloc[-1].minute == 31
    assert get_bars('600000.XSHG', 1, '1m', ['date','close'],
                    include_now=False, end_dt=c.current_dt, df=True).date.iloc[-1].minute == 33
    assert get_bars('600000.XSHG', 1, '1m', ['date','close'],
                    include_now=True, end_dt=c.current_dt, df=True).date.iloc[-1].minute == 34
    try: get_price('600000.XSHG', count=1, frequency='1m', fq=None,
                   end_date=c.current_dt.replace(minute=35))
    except ValueError as e: assert 'FUTURE_DATA_REQUEST' in str(e)
    else: raise AssertionError('future endpoint accepted')
""",
        doc=doc,
    )
    reads = [e for e in events if e["kind"] == "history_read" and "T09:31:" in e["cutoff"]]
    assert len(reads) == 3
    assert all(e["knowledge_at"] == "2024-03-04T09:34:00+08:00" for e in reads)


@pytest.mark.parametrize(
    "name", ["initialize", "before_trading_start", "handle_data", "after_trading_end", "scheduled"]
)
@pytest.mark.parametrize("kind", ["coroutine", "generator", "async_generator"])
def test_deferred_callback_definitions_fail_all_lifecycle_entries(run_minute, tmp_path, name, kind):
    prefix = "async " if kind != "generator" else ""
    body = "raise RuntimeError('must-not-be-silently-skipped')"
    if kind != "coroutine":
        body += "\n    yield 1"
    init = "" if name == "initialize" else "def initialize(c): pass\n"
    if name == "scheduled":
        init = "def initialize(c): run_daily(scheduled, 'open')\n"
    args = "c, data" if name == "handle_data" else "c"
    source = f"from jqdata import *\n{init}{prefix}def {name}({args}):\n    {body}\n"
    with pytest.raises(MinuteRunError, match="JQ_CALLBACK_UNSUPPORTED"):
        run_minute(source)
    assert not (tmp_path / "run/complete.json").exists()
    assert not (tmp_path / "run/result.json").exists()
    assert "never awaited" not in (tmp_path / "run/stderr.log").read_text()
    assert (
        "JQ_CALLBACK_UNSUPPORTED"
        in json.loads((tmp_path / "run/failure.json").read_text())["message"]
    )


@pytest.mark.parametrize("target", ["initialize", "scheduled"])
@pytest.mark.parametrize("kind", ["coroutine", "generator", "async_generator", "awaitable"])
def test_sync_wrappers_returning_deferred_execution_objects_fail(
    run_minute, tmp_path, target, kind
):
    definitions = {
        "coroutine": "async def deferred():\n    raise RuntimeError('not-awaited')\n",
        "generator": "def deferred():\n    raise RuntimeError('not-iterated')\n    yield 1\n",
        "async_generator": "async def deferred():\n    raise RuntimeError('not-iterated')\n    yield 1\n",
        "awaitable": "class deferred:\n    def __await__(self):\n        raise RuntimeError('not-awaited')\n        yield\n",
    }
    init = "" if target == "initialize" else "def initialize(c): run_daily(scheduled, 'open')\n"
    with pytest.raises(MinuteRunError, match="JQ_CALLBACK_RESULT_UNSUPPORTED"):
        run_minute(
            f"from jqdata import *\n{definitions[kind]}{init}def {target}(c): return deferred()\n"
        )
    assert not (tmp_path / "run/complete.json").exists()
    events = [json.loads(x) for x in (tmp_path / "run/events.jsonl").read_text().splitlines()]
    assert not any(e["kind"] == "callback_end" and e["name"] == target for e in events)
    assert "never awaited" not in (tmp_path / "run/stderr.log").read_text()


def posthoc(doc):
    doc = copy.deepcopy(doc)
    doc["state_visibility"] = "posthoc_daily_replay"
    doc["state_replay"] = {
        "model": "dated-daily-state-0900.v1",
        "purpose": "retrospective_research",
        "evidence": "engineered dated-state and receipt test evidence, not market evidence",
        "limitations": ["Historical first visibility and intraday state changes are not certified"],
    }
    for s in doc["states"]:
        s.update(
            available_at=None,
            historical_available_at=None,
            observed_at="2026-10-13T03:00:00+00:00",
            replay_effective_at=s["date"] + "T09:00:00+08:00",
        )
    return doc


@pytest.mark.parametrize(
    "mutation,opts,error",
    [
        (lambda d: None, {}, "STATE_REPLAY_REQUIRES_OPT_IN"),
        (lambda d: None, {"accept_assumed": True}, "STATE_REPLAY_REQUIRES_OPT_IN"),
        (
            lambda d: d["state_replay"].update(evidence=None),
            {"accept_state_replay": True},
            "REPLAY_EVIDENCE_REQUIRED",
        ),
        (
            lambda d: d["state_replay"].update(purpose="historical_pit"),
            {"accept_state_replay": True},
            "REPLAY_EVIDENCE_REQUIRED",
        ),
        (
            lambda d: d["states"][0].update(available_at="2024-03-04T09:00:00+08:00"),
            {"accept_state_replay": True},
            "PRESERVE_UNKNOWN_AVAILABILITY",
        ),
        (
            lambda d: d["states"][0].update(replay_effective_at="2024-03-05T09:00:00+08:00"),
            {"accept_state_replay": True},
            "SAME_DAY_0900",
        ),
        (
            lambda d: d["states"][0].update(observed_at=None),
            {"accept_state_replay": True},
            "AWARE_TIMESTAMP_REQUIRED",
        ),
        (
            lambda d: d["states"][0].update(suspended=None),
            {"accept_state_replay": True},
            "EXPLICIT_STATE_REQUIRED",
        ),
        (
            lambda d: d["minute"][0].update(available_at="2024-03-04T09:30:00+08:00"),
            {"accept_state_replay": True},
            "UNFINISHED_BAR_VISIBLE",
        ),
        (
            lambda d: d.update(visibility="assumed_closed"),
            {"accept_state_replay": True},
            "ASSUMED_VISIBILITY_REQUIRES_OPT_IN",
        ),
        (
            lambda d: d["action_coverage"].update(status="unknown"),
            {"accept_state_replay": True},
            "ACTION_COVERAGE_UNKNOWN",
        ),
    ],
)
def test_state_model_opt_in_never_waives_evidence_clocks_or_other_gates(
    minute_doc, tmp_path, mutation, opts, error
):
    doc = posthoc(minute_doc)
    mutation(doc)
    p = tmp_path / "input.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=error):
        MinuteInputs(p, **opts)


def test_unknown_strict_state_stays_rejected_with_either_opt_in(minute_doc, tmp_path):
    minute_doc["states"][0]["available_at"] = None
    p = tmp_path / "input.json"
    p.write_text(json.dumps(minute_doc))
    for opts in ({}, {"accept_assumed": True}, {"accept_state_replay": True}):
        with pytest.raises(ValueError, match="STATE_AVAILABILITY_UNKNOWN"):
            MinuteInputs(p, **opts)


def test_declared_clock_cannot_hide_explicitly_unknown_historical_time(minute_doc, tmp_path):
    minute_doc["states"][0]["historical_available_at"] = None
    p = tmp_path / "input.json"
    p.write_text(json.dumps(minute_doc))
    with pytest.raises(ValueError, match="STATE_AVAILABILITY_CONFLICT"):
        MinuteInputs(p, accept_state_replay=True)


def test_state_replay_clock_cannot_expose_future_date_or_preopen_state(minute_doc, tmp_path):
    p = tmp_path / "input.json"
    p.write_text(json.dumps(posthoc(minute_doc)))
    data = MinuteInputs(p, accept_state_replay=True)
    for day, at in [
        ("2024-03-04", "2024-03-04T08:59:59+08:00"),
        ("2024-03-05", "2024-03-04T15:30:00+08:00"),
    ]:
        with pytest.raises(ValueError, match="STATE_NOT_VISIBLE_AT_CLOCK"):
            data.state(SID, day, at=stamp(at))
    assert (
        data.state(SID, "2024-03-04", at=stamp("2024-03-04T09:00:00+08:00"))["available_at"] is None
    )


def test_state_replay_native_run_identity_and_future_prefix(run_minute, minute_doc, tmp_path):
    doc = posthoc(flat_window(minute_doc, days=2))
    source = """from jqdata import *
def initialize(c): run_daily(buy, 'open')
def buy(c): order_target('600000.XSHG', 100)
def before_trading_start(c):
    q = get_current_data()['600000.XSHG']
    record(limit=q.high_limit, close=q.last_price)
"""
    result, before, root = run_minute(source, doc=doc, accept_state_replay=True, name="base")
    assert result["state_replay_used"] and result["state_visibility"] == "posthoc_daily_replay"
    assert result["historical_pit_verified"] is False
    assert json.loads((root / "manifest.json").read_text())["accept_state_replay"] is True
    assert json.loads((root / "input.json").read_text())["states"][0]["available_at"] is None
    again = run_backtest(
        tmp_path / "base.py", tmp_path / "base.json", root, accept_state_replay=True
    )
    assert again["native_run"] is False
    with pytest.raises(ValueError, match="STATE_REPLAY_REQUIRES_OPT_IN"):
        run_backtest(tmp_path / "base.py", tmp_path / "base.json", root)
    changed = copy.deepcopy(doc)
    for s in changed["states"]:
        if s["date"] == "2024-03-05":
            s.update(upper_limit="200", previous_close="12")
    _, after, _ = run_minute(source, doc=changed, accept_state_replay=True, name="future_state")
    cutoff = "2024-03-04T15:30:00+08:00"
    assert [e for e in before if e["at"] <= cutoff] == [e for e in after if e["at"] <= cutoff]
