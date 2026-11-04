"""Observed prices cannot replace missing states, even before corporate reads."""

import pytest
from ashare_data.product.released_references import ReleasedReferences
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.core.validation import stamp
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("fixed_release", [
    {"execution_fields": ["tradestatus", "is_st"]},
    {"execution_fields": ["tradestatus", "is_st"], "missing_listings": ["600000.XSHG"]},
], indirect=True)
def test_missing_states_refuse_before_corporate_or_inferred_listing(
    daily_jobs, fixed_release, tmp_path, monkeypatch, frequency
):
    _, _, body = daily_jobs
    release, _ = fixed_release
    reads = []

    def forbidden(*args, **kwargs):
        reads.append(True)
        raise AssertionError("Unknown states must fail before corporate reference I/O")

    monkeypatch.setattr(ReleasedReferences, "query", forbidden)
    request = PythonCreate.model_validate({**body, "frequency": frequency})
    data = DemandInputs(release.receipt(), request, tmp_path / "task")
    try:
        sid, day = "600000.XSHG", data.days[0]
        raw = data.owner.prices([sid], data.previous[day], day)
        assert len(raw["rows"]) == 2
        with pytest.raises(DomainError) as error:
            data.component(sid, day)
        assert error.value.code == "PYTHON_STATE_GAP"
        assert error.value.details == {
            "security": sid, "date": day, "fields": ["tradestatus", "is_st"],
        }
        assert not reads and not data.components and not data.corporate
        assert all("suspended" not in row for row in data.rows.values() if row)
    finally:
        data.close()


@pytest.mark.parametrize("fixed_release", [{"warmup_fields": ["tradestatus", "is_st"]}], indirect=True)
def test_daily_execution_does_not_require_unused_warmup_status(daily_jobs, fixed_release, tmp_path):
    _, _, body = daily_jobs
    release, _ = fixed_release
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path / "task")
    try:
        sid, day = "600000.XSHG", data.days[0]
        part = data.component(sid, day)
        assert part.row(sid, day)["tradestatus"] == "1"
        assert part.row(sid, data.previous[day])["tradestatus"] is None
    finally:
        data.close()


@pytest.mark.parametrize("fixed_release", [
    {"minutes": True, "warmup_fields": ["tradestatus", "is_st"]},
], indirect=True)
def test_minute_execution_uses_warmup_close_without_inventing_warmup_states(
    daily_jobs, fixed_release, tmp_path
):
    _, _, body = daily_jobs
    release, _ = fixed_release

    request = PythonCreate.model_validate({**body, "frequency": "1m"})
    data = DemandInputs(release.receipt(), request, tmp_path / "task")
    try:
        sid, day = "600000.XSHG", data.days[0]
        previous = data.previous[day]
        part = data.component(sid, day)
        warmup = data._row(sid, previous)
        assert warmup["tradestatus"] is None and warmup["is_st"] is None
        assert "suspended" not in warmup
        assert len(part.bars) == 240
        assert [(s["security"], s["date"]) for s in part.document["states"]] == [(sid, day)]
        state = part.document["states"][0]
        assert state["previous_close"] == warmup["close"]
        assert state["previous_close_observation"]["date"] == previous
        at = stamp(day + "T09:50:00+08:00")
        assert data.daily_history(sid, at, count=1, fields=("close",), skip_paused=False) == [warmup]
        with pytest.raises(DomainError) as error:
            data.daily_history(sid, at, count=1, fields=("close",), skip_paused=True)
        assert error.value.code == "PYTHON_DAILY_HISTORY_STATE_GAP"
        assert error.value.details["date"] == previous
    finally:
        data.close()


@pytest.mark.parametrize("fixed_release", [
    {"minutes": True, "warmup_fields": ["tradestatus", "is_st"]},
], indirect=True)
def test_native_minute_trades_with_only_execution_day_states(daily_jobs, fixed_release):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.pop("securities")
    body.update(frequency="1m", end_date=days[21], source="""from aquant import *
def initialize(context):
    set_universe(['600000.XSHG'])
    set_benchmark(None)
    run_daily(rebalance, time='09:50')
def rebalance(context):
    target = 100 if context.current_dt.date() == context.run_params.start_date else 0
    order_target('600000.XSHG', target)
""")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        assert client.get(job["result"]["href"]).json()["summary"]["trade_count"] == 2


@pytest.mark.parametrize("fixed_release", [
    {"minutes": True, "warmup_fields": ["close", "tradestatus", "is_st"]},
], indirect=True)
def test_minute_execution_still_requires_warmup_close(daily_jobs, fixed_release, tmp_path):
    _, _, body = daily_jobs
    release, _ = fixed_release
    request = PythonCreate.model_validate({**body, "frequency": "1m"})
    data = DemandInputs(release.receipt(), request, tmp_path / "task")
    try:
        with pytest.raises(ValueError, match="MINUTE_NUMBER_TYPE"):
            data.component("600000.XSHG", data.days[0])
    finally:
        data.close()
