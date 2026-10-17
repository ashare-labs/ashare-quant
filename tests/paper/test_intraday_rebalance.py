"""Synthetic Monday inputs, real native ledger. Never real-calendar evidence."""

import copy
import json
from datetime import datetime
from decimal import Decimal as D

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday, online
from ashare_quant.paper.intraday_contracts import REBALANCE_MODEL, UNIVERSE, IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.risk import order_admission
from ashare_quant.storage.artifacts import digest

DAY = "2026-10-19"
PRICES = dict(zip(UNIVERSE, ("10", "20", "30"), strict=True))


def setup(tmp_path, monkeypatch, *, inventory=None, targets=None):
    clock = {"at": datetime.fromisoformat(DAY + "T09:35:31+08:00")}
    for mod in (intraday, online):
        monkeypatch.setattr(mod, "now", lambda: clock["at"])
    daily = dict(
        signal_day="2026-10-16",
        signal_ready=True,
        revisions=[],
        completed_at="2026-10-16T18:00:00+08:00",
        calendar=dict(
            next_open=DAY,
            open_days=["2026-10-16", DAY, "2026-10-20"],
            source=dict(observed_at="2026-10-16T18:00:00+08:00"),
        ),
        securities={
            s: dict(
                rows=[dict(open=p, high=p, low=p, close=p, volume="1000000")] * 120,
                source=dict(observed_at="2026-10-16T18:00:00+08:00"),
            )
            for s, p in PRICES.items()
        },
    )
    plan = IntradayPlan(
        schema_version="paper-intraday-plan.v3",
        model=REBALANCE_MODEL,
        strategy_id="acceptance-rebalance",
        day=DAY,
        start_at=DAY + "T09:35:00+08:00",
        intent_deadline=DAY + "T09:40:00+08:00",
        execution_deadline=DAY + "T09:50:00+08:00",
        admission_reader_sha256=digest("synthetic-owner"),
        action_baseline_sha256=digest("synthetic-action-review"),
        daily_capture=digest(daily),
        remainder_policy="cancel_after_minute",
        initial_inventory=dict(
            as_of="2026-10-16",
            quantities=inventory or dict(zip(UNIVERSE, (300, 200, 0), strict=True)),
        ),
        target_quantities=targets or dict(zip(UNIVERSE, (0, 100, 200), strict=True)),
    )

    class Reader:
        offline = False

        def read(self, config, kind, cap):
            if self.offline:
                raise ConnectionError("controlled offline read; no network")
            return copy.deepcopy(self.packets[cap])

        intraday = read

    # Match the production intraday reader signature, but explicitly substitute
    # owner transport. AD raw-response verification is exercised in AD tests.
    class Phases(Reader):
        def intraday(self, config, cap):
            return self.read(config, "intraday", cap)

    reader = Phases()
    reader.packets = {digest(daily): daily}
    ids = {}
    for kind, at, after in (
        ("admission", "09:35:30", "09:35:00"),
        ("execution", "09:38:30", "09:35:31"),
    ):
        rows = []
        for sid, p in PRICES.items():
            proof = dict(baseline_sha256=plan.action_baseline_sha256, security=sid, day=DAY)
            proof = dict(proof, status="proven", source_sha256=digest(proof))
            row = dict(
                security=sid,
                eligible=True,
                suspended=False,
                is_st=False,
                reference_close=p,
                lower_limit=str(D(p) * D(".9")),
                upper_limit=str(D(p) * D("1.1")),
                rule_version="synthetic-rule",
                actions_known_absent=False,
                action_scope=dict(
                    security=sid,
                    from_day="2026-10-16",
                    through_day=DAY,
                    price_share_code_review=proof,
                ),
                cash_inventory=dict(
                    cash_history_complete=True,
                    unresolved=[],
                    events=[
                        dict(type="cash_dividend", record_date="2026-01-01", ex_date="2026-01-02")
                    ],
                ),
            )
            if kind == "execution":
                row["execution"] = dict(
                    minute_end=DAY + "T09:37:00+08:00",
                    raw_close=p,
                    match_price=str(D(p) + D(".01")),
                    volume=1000000,
                    source_sha256=digest([sid, "source"]),
                    confirmation_sha256=digest([sid, "repeat"]),
                    observed_at=DAY + "T09:38:30+08:00",
                )
            rows.append(row)
        packet = dict(
            schema="ad-intraday-observation.v3",
            evidence_kind="sealed_response_replay",
            kind=kind,
            day=DAY,
            completed_at=DAY + "T" + at + "+08:00",
            not_before=DAY + "T" + after + "+08:00",
            assessment=dict(ready=True, blockers=[], rows=rows),
        )
        ids[kind] = digest(packet)
        reader.packets[ids[kind]] = packet
    service = OnlineService(tmp_path / "account")
    service.create(
        intraday.config_for(
            plan, "replay-only-monday-rebalance", response_replay_sha256=digest("controlled test")
        )
    )
    return service, reader, plan, ids, clock


def open_account(service, reader, plan, ids, clock):
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat(DAY + "T09:38:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])


@pytest.mark.parametrize(
    "fault", [None, "inside_native_trade", "inside_native_submit", "after_opening_checkpoint"]
)
def test_rotation_native_inventory_fees_t1_cash_and_restart(tmp_path, monkeypatch, fault):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    open_account(service, reader, plan, ids, clock)
    if fault:
        with pytest.raises(DomainError, match="exited -9"):
            service.advance(DAY, "opening", fault=fault)
        service = OnlineService(service.root)
        service.resume("controlled crash; same sealed Monday input")
    report = service.advance(DAY, "opening")
    native = report["native"]
    assert native["genesis"]["prior_trades_claimed"] is False
    assert native["before"]["total_cash"] == "30000.0"
    assert {
        s: p["quantity"] for s, p in native["before"]["positions"].items()
    } == plan.initial_inventory.quantities
    trades = native["trades"]
    assert [(t["side"], t["quantity"], t["price"]) for t in trades] == [
        ("SELL", 300, "9.99"),
        ("SELL", 100, "19.99"),
        ("BUY", 200, "30.01"),
    ]
    assert len({t["id"] for t in trades}) == 3
    for t in trades:
        cost = D(t["price"]) * t["quantity"]
        assert D(t["commission"]) == max(D(5), cost * D(".0003"))
        assert abs(D(t["tax"]) - (cost * D(".0005") if t["side"] == "SELL" else 0)) < D("1e-8")
    cash = D(30000) + sum(
        (1 if t["side"] == "SELL" else -1) * D(t["price"]) * t["quantity"] - D(t["cost"])
        for t in trades
    )
    account = native["account"]
    assert abs(D(account["total_cash"]) - cash) < D("1e-8")
    assert {s: p["quantity"] for s, p in account["positions"].items()} == plan.target_quantities
    assert account["positions"][UNIVERSE[1]]["sellable"] == 100
    assert account["positions"][UNIVERSE[2]]["sellable"] == 0
    assert D(account["frozen_cash"]) == 0 and not account["open_orders"]
    _, config = service._load()
    with pytest.raises(DomainError, match="PAPER_T1_LIMIT"):
        order_admission(config, account, UNIVERSE[2], -100, "30.01")
    before = service.path.read_bytes()
    check = (service.root / DAY / "opening.chk").read_bytes()
    intraday.deliver(service, reader, plan, ids["execution"])
    assert OnlineService(service.root).advance(DAY, "opening") == report
    assert (
        service.path.read_bytes() == before
        and (service.root / DAY / "opening.chk").read_bytes() == check
    )
    (tmp_path / "controlled-result.json").write_text(
        json.dumps(dict(real_calendar_acceptance=False, report=report), indent=2)
    )


def test_unfilled_sells_cannot_fund_buy(tmp_path, monkeypatch):
    inventory = dict(zip(UNIVERSE, (10000, 10000, 0), strict=True))
    targets = dict(zip(UNIVERSE, (0, 0, 1200), strict=True))
    service, reader, plan, ids, clock = setup(
        tmp_path, monkeypatch, inventory=inventory, targets=targets
    )
    for r in reader.packets[ids["execution"]]["assessment"]["rows"][:2]:
        r["execution"]["volume"] = 9999
    open_account(service, reader, plan, ids, clock)
    native = service.advance(DAY, "opening")["native"]
    assert native["trades"] == [] and len(native["orders"]) == 2
    assert native["refusals"][0]["code"] == "PAPER_CASH_BUFFER"
    assert D(native["account"]["total_cash"]) == 30000
    assert all(o["status"] == "CANCELLED" for o in native["orders"].values())


@pytest.mark.parametrize("fault", [None, "inside_native_settlement", "after_complete_checkpoint"])
def test_data_ready_close_before_1505_and_resume(tmp_path, monkeypatch, fault):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    open_account(service, reader, plan, ids, clock)
    opening = service.advance(DAY, "opening")
    daily = copy.deepcopy(reader.packets[plan.daily_capture])
    daily.update(
        signal_day=DAY,
        completed_at=DAY + "T15:00:40+08:00",
        schema="ad-current-daily-observation.v2",
        close_confirmation=dict(policy="sse-ended-and-repeat-daily-agrees.v1"),
    )
    for item in daily["securities"].values():
        item["source"]["observed_at"] = DAY + "T15:00:20+08:00"
    cap = digest(daily)
    reader.packets[cap] = daily
    clock["at"] = datetime.fromisoformat(DAY + "T15:00:41+08:00")
    service.deliver_observed(reader, DAY, "closing", cap)
    if fault:
        with pytest.raises(DomainError, match="exited -9"):
            service.advance(DAY, "closing", fault=fault)
        service = OnlineService(service.root)
        service.resume("controlled closing crash")
    closing = service.advance(DAY, "closing")
    assert closing["native"]["trades"] == opening["native"]["trades"]
    assert closing["native"]["settlement_count"] == 1
    # T+1 unlock is at the next trading day's preparation, not Monday's close.
    assert closing["native"]["account"]["positions"][UNIVERSE[2]]["sellable"] == 0
    state = service.path.read_bytes()
    service.deliver_observed(reader, DAY, "closing", cap)
    assert OnlineService(service.root).advance(DAY, "closing") == closing
    assert service.path.read_bytes() == state


def test_weekend_offline_and_halt_block_before_orders(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    state = service.path.read_bytes()
    reader.offline = True
    with pytest.raises(ConnectionError):
        intraday.prepare(service, reader, plan, ids["admission"])
    reader.offline = False
    clock["at"] = datetime.fromisoformat("2026-10-17T09:35:31+08:00")
    with pytest.raises(DomainError):
        intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat(DAY + "T09:35:31+08:00")
    reader.packets[ids["admission"]]["assessment"]["rows"][0]["suspended"] = True
    with pytest.raises(DomainError, match="PAPER_TRADING_STATE_BLOCKED"):
        intraday.prepare(service, reader, plan, ids["admission"])
    assert service.path.read_bytes() == state and not list(service.root.rglob("*.chk"))


def test_declaration_change_cannot_reuse_account(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    changed = plan.model_dump(mode="json")
    changed["target_quantities"][UNIVERSE[2]] = 100
    with pytest.raises(DomainError, match="PAPER_INTRADAY_PLAN_IDENTITY"):
        intraday.prepare(service, reader, changed, ids["admission"])


@pytest.mark.parametrize(
    "sid,raw,side", [(UNIVERSE[0], "9.01", "SELL"), (UNIVERSE[2], "32.99", "BUY")]
)
def test_price_limit_refusal_keeps_other_actual_native_fills(tmp_path, monkeypatch, sid, raw, side):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    for r in reader.packets[ids["execution"]]["assessment"]["rows"]:
        if r["security"] == sid:
            r["execution"]["raw_close"] = raw
            r["execution"]["match_price"] = str(D(raw) + D(".01"))
    open_account(service, reader, plan, ids, clock)
    native = service.advance(DAY, "opening")["native"]
    assert len(native["trades"]) == 2 and all(t["security"] != sid for t in native["trades"])
    order = next(o for o in native["orders"].values() if o["security"] == sid)
    assert (
        order["side"] == side
        and order["filled"] == 0
        and order["status"] in {"REJECTED", "CANCELLED"}
    )
    assert native["account"]["open_orders"] == [] and D(native["account"]["frozen_cash"]) == 0
