"""Continuous research scope and native cash rights, without market fixtures."""

import json
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from test_research_contract import ACCEPT, make_document, save

from ashare_quant.backtests.minute import MinuteInputs, run_backtest
from ashare_quant.backtests.minute.inputs import stamp
from ashare_quant.backtests.minute.research import (
    CORPORATE_ASSUMPTIONS,
    CORPORATE_PROFILE,
    LIMIT_MODEL,
    daily_model_bounds,
    digest,
)
from ashare_quant.portfolio.corporate import MODEL, STAGES

SID = "601234.XSHG"


def corporate_document(bundle, doc):
    doc["research"].update(
        profile=CORPORATE_PROFILE, assumptions=CORPORATE_ASSUMPTIONS, envelope=LIMIT_MODEL
    )
    doc["research"]["capabilities"]["corporate_actions"] = "source_scoped_cash_dates"
    doc["corporate_account_model"] = MODEL
    doc["corporate_schedule"] = dict(
        schema="ad.aq-corporate-actions.v1", binding_sha256="a"*64, event_identity_version=2,
        source_scoped_coverage_accepted=True, tax_model="gross_cash_no_tax",
        ready_for_declared_date_model=True, issues=[], historical_pit=False,
        strategy_visible=False, execution_permission=False, economic_result_certified=False,
        rows=[], coverage=dict(
            binding_sha256="a"*64, start=doc["calendar"][0], end=doc["days"][-1],
            rows=[dict(
                security=s, provider_tables_captured=True, economic_schedule_fields_complete=True,
                events=0, source_gaps=[], event_field_gaps=[], identity_boundaries=[],
                interval_state="empty_source_scoped_interval",
            ) for s in doc["securities"]],
        ),
    )
    doc["action_coverage"] = dict(
        status="source_scoped_corporate_date_model", complete=False, absence_proved=False,
        historical_pit=False, binding_sha256="a"*64,
        response_sha256=digest(doc["corporate_schedule"]), evidence=["fixture"],
    )
    for state in doc["states"]:
        raw = dict(
            date=state["date"], symbol=("sh" if state["security"].endswith("XSHG") else "sz")
            + state["security"][:6], tradestatus="1", is_st="0", preclose="10",
            source="constructed", observed_at=state["observed_at"],
        )
        state["daily_limit_model"] = dict(
            schema=LIMIT_MODEL, source_row=raw, source_row_sha256=digest(raw),
            source_refs=["fixture"], exchange_limits_verified=False,
        )


def cash_dividend(bundle, doc):
    corporate_document(bundle, doc)
    key = SID+":synthetic-cash"
    action = dict(
        security=SID, type="distribution", event_id=key, entitlement_key=key,
        settlement_keys={s:key+":"+s for s in STAGES}, revision_observation_ids=["fixture"],
        conflicts=[], identity_issues=[], record_date="2025-07-02", ex_date="2025-07-03",
        payment_date="2025-07-04", cash_per_share="0.26",
        bonus_share_ratio="0", capitalization_share_ratio="0",
    )
    schedule = doc["corporate_schedule"]
    schedule["rows"] = [action]
    schedule["coverage"]["rows"][0].update(events=1, interval_state="events_observed")
    doc["action_coverage"]["response_sha256"] = digest(schedule)
    for state in doc["states"]:
        if state["date"] >= "2025-07-03":
            model = state["daily_limit_model"]
            model["source_row"]["preclose"] = "9.74"
            model["source_row_sha256"] = digest(model["source_row"])
            if state["date"] > "2025-07-03":
                state["previous_close"] = "9.74"
                state["source_observation"]["previous_close"]["value"] = "9.74"
                state["previous_close_observation"]["close"] = "9.74"
    for bar in bundle["bars"]:
        if bar["timestamp"][:10] >= "2025-07-03":
            bar.update({k:"9.74" for k in ("open","high","low","close")}, amount="97400")
        if bar["timestamp"] == "2025-07-04T09:32:00+08:00":
            bar.update(volume="0",amount="0")


def test_long_scope_remains_explicit_and_indexed(tmp_path, monkeypatch):
    cal = [(date(2025,1,1)+timedelta(days=n)).isoformat() for n in range(63)]
    doc = make_document(tmp_path, ids=(SID,), calendar=cal, edit=corporate_document)
    data = MinuteInputs(save(doc,tmp_path/"input.json"), **ACCEPT)
    assert len(data.days)==61 and data.corporate_actions==[]
    assert data.preflight()["strict_execution_admitted"] is False
    monkeypatch.setattr(data, "visible", lambda *a,**k: pytest.fail("unbounded history scan"))
    mark = data.valuation_mark(SID,stamp(cal[-2]+"T09:33:00+08:00"))
    assert mark["price"]==10 and mark["status"]=="research_mark"


@pytest.mark.parametrize("field,value,error", [
    ("exchange_limits_verified",True,"SOURCE_BINDING"),
    ("source_row_sha256","b"*64,"SOURCE_BINDING"),
])
def test_model_is_not_source_certification(tmp_path, field, value, error):
    doc=make_document(tmp_path,ids=(SID,),edit=corporate_document)
    doc["states"][0]["daily_limit_model"][field]=value
    with pytest.raises(ValueError,match=error):
        MinuteInputs(save(doc,tmp_path/"input.json"),**ACCEPT)


def test_bounds_do_not_inherit_native_decimal_precision():
    state=dict(is_st=False,daily_limit_model=dict(source_row=dict(preclose="123456.7545")))
    with localcontext(prec=10):
        bounds=daily_model_bounds(state)
    assert bounds==(Decimal("111111.08"),Decimal("135802.43"))


def test_cash_profile_rejects_incomplete_coverage_and_share_distribution(tmp_path):
    cal=[f"2025-07-0{n}" for n in range(1,7)]
    doc=make_document(tmp_path,ids=(SID,),calendar=cal,edit=cash_dividend)
    schedule=doc["corporate_schedule"]
    schedule["coverage"]["rows"][0]["source_gaps"]=["uncaptured"]
    doc["action_coverage"]["response_sha256"]=digest(schedule)
    with pytest.raises(ValueError,match="CORPORATE_COVERAGE_GAP"):
        MinuteInputs(save(doc,tmp_path/"gap.json"),**ACCEPT)
    schedule["coverage"]["rows"][0]["source_gaps"]=[]
    schedule["rows"][0].update(bonus_share_ratio="0.1",share_credit_date="2025-07-03",
                               new_shares_tradable_date="2025-07-04")
    doc["action_coverage"]["response_sha256"]=digest(schedule)
    with pytest.raises(ValueError,match="MINUTE_CORPORATE_ONLY_CASH"):
        MinuteInputs(save(doc,tmp_path/"shares.json"),**ACCEPT)


def test_record_ex_payment_and_zero_capacity_use_native_account(tmp_path):
    cal=[f"2025-07-0{n}" for n in range(1,7)]
    doc=make_document(tmp_path,ids=(SID,),calendar=cal,edit=cash_dividend)
    path=save(doc,tmp_path/"input.json")
    data=MinuteInputs(path,**ACCEPT)
    opening=data.valuation_mark(SID,stamp("2025-07-03T09:30:00+08:00"))
    assert opening["price"]==9.74 and opening["status"]=="daily_reference_model"
    strategy=tmp_path/"strategy.py"
    strategy.write_text("""from aquant import order, run_daily
def initialize(context): run_daily(trade, '09:31')
def trade(context):
    day=str(context.current_dt.date())
    if day in ('2025-07-02','2025-07-04'): order('601234.XSHG',100)
    if day=='2025-07-03': order('601234.XSHG',-100)
""")
    root=tmp_path/"run"
    result=run_backtest(strategy,path,root,**ACCEPT)["result"]
    assert [o["status"] for o in result["orders"]]==["filled","filled","canceled"]
    assert len(result["trades"])==2
    assert result["final_account"]["available_cash"]==pytest.approx(99989.026,abs=1e-8)
    assert not result["final_account"]["positions"]
    accounts=[json.loads(s) for s in (root/"events.jsonl").read_text().splitlines()
              if '"kind": "account"' in s]
    ex=next(e for e in accounts if e["at"]=="2025-07-03T09:30:00+08:00")
    assert ex["positions"][SID]["avg_cost"]==9.74
    assert ex["positions"][SID]["dividend_receivable"]==26
    assert ex["total_value"]==pytest.approx(99995)
    sold=next(e for e in accounts if e["at"]=="2025-07-03T09:33:00+08:00")
    assert sold["positions"][SID]["total_amount"]==0
    assert sold["positions"][SID]["dividend_receivable"]==26
    rights=next(iter(result["corporate_entitlements"][SID].values()))
    assert rights["basis"]==100 and rights["cash"]=="26.00"
    assert set(rights["stages"].values())=={"2025-07-02","2025-07-03","2025-07-04"}
