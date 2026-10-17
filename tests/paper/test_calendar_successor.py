"""Controlled future receipts against a COPY of a settled native checkpoint.

ASHARE_SUCCESSOR_ACCOUNT selects separately held, read-only source evidence.
No source account writes, networking, actual-day claims or production clock
overrides are provided by these tests. Native processes are real.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import calendar_successor as successor
from ashare_quant.paper import intraday, online
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.service import engine_pin
from ashare_quant.storage.artifacts import atomic, digest, sha


def files(root):
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = os.environ.get("ASHARE_SUCCESSOR_ACCOUNT")
    if not source:
        pytest.skip(
            "Explicit separately held settled account required; never creates market evidence"
        )
    source = Path(source)
    before = files(source)
    root = tmp_path / "account-copy"
    shutil.copytree(source, root)
    service = OnlineService(root)
    state, config = service._load(verify=False)
    assert state["last_day"] == "2026-10-15"
    report = service._report(state, state["last_day"], "closing")
    old_plan = state["days"][state["last_day"]]["decision"]["strategy_receipt"]["intraday_plan"]
    baseline = digest("controlled successor review, not real issuer evidence")
    pin = digest("controlled upgraded owner, no transport")
    clock = {"at": datetime.fromisoformat("2026-10-15T19:00:00+08:00")}
    for module in (successor, intraday, online):
        monkeypatch.setattr(module, "now", lambda: clock["at"])

    class Reader:
        def __init__(self):
            self.packets = {}
            self.offline = False

        def reviewed_successor(self):
            return dict(
                first_window=["2026-10-14", "2026-10-20"],
                admission_reader_sha256=pin,
                action_baseline_sha256=baseline,
                reviewed_at="2026-10-15T18:00:00+08:00",
                baseline_lineage=[old_plan["action_baseline_sha256"], baseline],
            )

        def read(self, cfg, kind, capture):
            assert cfg.admission_reader_sha256 == pin and kind == "daily"
            if self.offline:
                raise ConnectionError("controlled offline owner read")
            return copy.deepcopy(self.packets[capture])

        def intraday(self, cfg, capture):
            assert cfg.admission_reader_sha256 == pin
            if self.offline:
                raise ConnectionError("controlled offline owner read")
            return copy.deepcopy(self.packets[capture])

    reader = Reader()
    prices = {s: p["price"] for s, p in report["native"]["account"]["positions"].items()}

    def daily(day, next_day, after):
        at = day + "T17:45:00+08:00"
        return dict(
            signal_day=day,
            signal_ready=True,
            revisions=[],
            completed_at=at,
            calendar=dict(
                next_open=next_day, open_days=[day, next_day, after], source=dict(observed_at=at)
            ),
            securities={
                s: dict(
                    rows=[dict(open=p, high=p, low=p, close=p, volume=1000000)] * 120,
                    source=dict(observed_at=at),
                )
                for s, p in prices.items()
            },
        )

    # Source hash is the settled parent's selected observation. The read layer
    # is explicitly substituted here; real owner validation is tested separately.
    reader.packets[old_plan["daily_capture"]] = daily("2026-10-14", "2026-10-15", "2026-10-16")
    capture = state["days"]["2026-10-15"]["close"]["data_manifest_sha256"]
    reader.packets[capture] = daily("2026-10-15", "2026-10-16", "2026-10-19")

    def phases(plan):
        day = str(plan.day)
        old_facts = state["days"]["2026-10-15"]["decision"]["strategy_receipt"][
            "action_adjudications"
        ]
        ids = {}
        for kind, received, after in [
            ("admission", "10:00:30", "10:00:00"),
            ("execution", "10:03:30", "10:00:31"),
        ]:
            rows = []
            for sid, p in prices.items():
                old = copy.deepcopy(old_facts[sid])
                scope = old["action_scope"]
                proof = scope["price_share_code_review"]
                proof.update(
                    baseline_sha256=baseline,
                    day=day,
                    fresh_check_completed_at=day + "T" + received + "+08:00",
                )
                proof["source_sha256"] = digest(
                    {k: v for k, v in proof.items() if k not in {"status", "source_sha256"}}
                )
                scope["through_day"] = "2026-10-20"
                row = dict(
                    security=sid,
                    eligible=True,
                    suspended=False,
                    is_st=False,
                    reference_close=p,
                    lower_limit=str(Decimal(p) * Decimal(".9")),
                    upper_limit=str(Decimal(p) * Decimal("1.1")),
                    rule_version="controlled-copy-fixture",
                    actions_known_absent=False,
                    action_scope=scope,
                    cash_inventory=dict(
                        events=old["events"],
                        cash_history_complete=True,
                        unresolved=[],
                        proposals=[],
                    ),
                )
                if kind == "execution":
                    row["execution"] = dict(
                        minute_end=day + "T10:02:00+08:00",
                        raw_close=p,
                        match_price=str(Decimal(p) + Decimal(".01")),
                        volume=1000000,
                        source_sha256=digest([day, sid]),
                        confirmation_sha256=digest([day, sid, "confirm"]),
                        observed_at=day + "T10:03:30+08:00",
                    )
                rows.append(row)
            packet = dict(
                schema="ad-intraday-observation.v3",
                evidence_kind="live_local_capture",
                kind=kind,
                day=day,
                completed_at=day + "T" + received + "+08:00",
                not_before=day + "T" + after + "+08:00",
                assessment=dict(ready=True, blockers=[], rows=rows),
            )
            ids[kind] = digest(packet)
            reader.packets[ids[kind]] = packet
        return ids

    result = dict(
        service=service,
        reader=reader,
        clock=clock,
        daily=daily,
        phases=phases,
        baseline=baseline,
        source=source,
        source_hashes=before,
        original=state,
        account=report["native"]["account"],
    )
    yield result
    assert files(source) == before


def bind_day(c, day, stage=None):
    s = c["service"]
    state, _ = s._load(verify=False)
    plan = successor.build_plan(s, c["reader"], day + "T10:00:00+08:00")
    args = dict(
        state_sha=sha(s.path.read_bytes()),
        from_runtime_sha=digest(successor.current_authority(state)),
        runtime_sha=digest(engine_pin()),
        review_sha=digest("controlled copy review"),
    )
    successor.bind(s, c["reader"], plan, **args, stage=stage)
    return plan, args


def run_day(c, day, next_day, after, fault=None):
    plan, args = bind_day(c, day)
    s = c["service"]
    reader = c["reader"]
    ids = c["phases"](plan)
    c["clock"]["at"] = datetime.fromisoformat(day + "T10:00:31+08:00")
    row = intraday.prepare(s, reader, plan, ids["admission"])
    before = s.path.read_bytes()
    assert (
        intraday.prepare(s, reader, plan, ids["admission"]) == row and s.path.read_bytes() == before
    )
    c["clock"]["at"] = datetime.fromisoformat(day + "T10:03:31+08:00")
    intraday.deliver(s, reader, plan, ids["execution"])
    if fault and fault.startswith("open:"):
        with pytest.raises(DomainError, match="exited -9"):
            s.advance(day, "opening", fault=fault.removeprefix("open:"))
        s = OnlineService(s.root)
        s.resume("controlled copy opening interruption")
    opened = s.advance(day, "opening")
    before = files(s.root)
    assert s.advance(day, "opening") == opened
    intraday.deliver(s, reader, plan, ids["execution"])
    assert files(s.root) == before
    n = opened["native"]
    assert n["orders"] == {} and n["trades"] == []
    assert n["account"]["total_cash"] == c["account"]["total_cash"]
    assert {k: v["quantity"] for k, v in n["account"]["positions"].items()} == {
        k: v["quantity"] for k, v in c["account"]["positions"].items()
    }
    assert all(p["quantity"] == p["sellable"] for p in n["account"]["positions"].values())
    packet = c["daily"](day, next_day, after)
    cap = digest(packet)
    reader.packets[cap] = packet
    c["clock"]["at"] = datetime.fromisoformat(day + "T17:45:01+08:00")
    s.deliver_observed(reader, day, "closing", cap)
    if fault and fault.startswith("close:"):
        with pytest.raises(DomainError, match="exited -9"):
            s.advance(day, "closing", fault=fault.removeprefix("close:"))
        s = OnlineService(s.root)
        s.resume("controlled copy closing interruption")
    closed = s.advance(day, "closing")
    before = files(s.root)
    s.deliver_observed(reader, day, "closing", cap)
    assert s.advance(day, "closing") == closed and files(s.root) == before
    c["service"] = s
    return opened, closed


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "open:event_committed:1",
        "open:after_opening_checkpoint",
        "close:inside_native_settlement",
        "close:after_complete_checkpoint",
    ],
)
def test_third_day_and_weekend_preserve_native_account(context, fault):
    c = context
    opened, closed = run_day(c, "2026-10-16", "2026-10-19", "2026-10-20", fault)
    assert opened["native"]["cursor"] == 10 and closed["native"]["cursor"] == 12
    assert opened["native"]["settlement_count"] == 2 and closed["native"]["settlement_count"] == 3
    # Saturday and Sunday are skipped from the owner calendar, not by a +1 rule.
    c["clock"]["at"] = datetime.fromisoformat("2026-10-16T18:00:00+08:00")
    opened, closed = run_day(c, "2026-10-19", "2026-10-20", "2026-10-21")
    assert opened["native"]["cursor"] == 14 and closed["native"]["cursor"] == 16
    assert closed["native"]["settlement_count"] == 4
    for day in ("2026-10-14", "2026-10-15"):
        assert files(c["service"].root / day) == files(c["source"] / day)
    atomic(
        c["service"].root.parent / "controlled-result.json",
        dict(
            real_calendar_acceptance=False,
            evidence_kind="controlled_copy_only",
            day16_and_weekend=True,
            final_report=closed,
        ),
    )


@pytest.mark.parametrize("stage", ["origin_saved", "binding_saved"])
def test_binding_interruption_and_retry_is_idempotent(context, stage):
    c = context
    s = c["service"]
    plan = successor.build_plan(s, c["reader"], "2026-10-16T10:00:00+08:00")
    old, _ = s._load(verify=False)
    args = dict(
        state_sha=sha(s.path.read_bytes()),
        from_runtime_sha=digest(successor.current_authority(old)),
        runtime_sha=digest(engine_pin()),
        review_sha=digest("controlled interrupted review"),
    )

    def stop(at):
        if at == stage:
            raise RuntimeError("controlled kill boundary")

    with pytest.raises(RuntimeError, match="controlled kill"):
        successor.bind(s, c["reader"], plan, **args, stage=stop)
    successor.bind(s, c["reader"], plan, **args)
    bound = files(s.root)
    successor.bind(OnlineService(s.root), c["reader"], plan, **args)
    assert files(s.root) == bound
    assert (
        sum(
            e["kind"] == "holding_successor_bound" and e["day"] == "2026-10-16"
            for e in json.loads(s.path.read_bytes())["events"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "damage",
    ["skip_open", "weekend", "revision", "not_ready", "old_runtime", "parent_file", "state_hash"],
)
def test_invalid_successor_cannot_change_account(context, damage):
    c = context
    s = c["service"]
    reader = c["reader"]
    plan = successor.build_plan(s, reader, "2026-10-16T10:00:00+08:00")
    old, _ = s._load(verify=False)
    args = dict(
        state_sha=sha(s.path.read_bytes()),
        from_runtime_sha=digest(successor.current_authority(old)),
        runtime_sha=digest(engine_pin()),
        review_sha=digest("controlled refusal"),
    )
    if damage in ("skip_open", "weekend"):
        day = "2026-10-19" if damage == "skip_open" else "2026-10-17"
        plan = IntradayPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "day": day,
                "start_at": day + "T10:00:00+08:00",
                "intent_deadline": day + "T10:05:00+08:00",
                "execution_deadline": day + "T10:10:00+08:00",
            }
        )
    if damage == "revision":
        reader.packets[plan.daily_capture]["revisions"] = ["changed close"]
    if damage == "not_ready":
        reader.packets[plan.daily_capture]["signal_ready"] = False
    if damage == "old_runtime":
        args["runtime_sha"] = args["from_runtime_sha"]
    if damage == "state_hash":
        args["state_sha"] = "0" * 64
    if damage == "parent_file":
        p = s.root / "2026-10-15" / "closing.chk"
        p.write_bytes(p.read_bytes() + b"changed")
    before = files(s.root)
    with pytest.raises(DomainError):
        successor.bind(s, reader, plan, **args)
    assert files(s.root) == before


def test_offline_read_retry_and_request_cursor_cannot_reset(context):
    c = context
    plan, _ = bind_day(c, "2026-10-16")
    s = c["service"]
    ids = c["phases"](plan)
    c["clock"]["at"] = datetime.fromisoformat("2026-10-16T10:00:31+08:00")
    before = files(s.root)
    c["reader"].offline = True
    with pytest.raises(ConnectionError):
        intraday.prepare(s, c["reader"], plan, ids["admission"])
    assert files(s.root) == before
    c["reader"].offline = False
    intraday.prepare(s, c["reader"], plan, ids["admission"])
    c["clock"]["at"] = datetime.fromisoformat("2026-10-16T10:03:31+08:00")
    intraday.deliver(s, c["reader"], plan, ids["execution"])
    s.advance(str(plan.day), "opening")
    request = json.loads((s.root / str(plan.day) / "opening-request.json").read_bytes())
    from ashare_quant.paper.online_contracts import OnlineDecision, OnlineSession

    request["base_cursor"] = 4
    with pytest.raises(DomainError, match="PAPER_SUCCESSOR_RUNTIME_CHANGED"):
        successor.verify_request(
            request,
            OnlineSession.model_validate(request["session"]),
            OnlineDecision.model_validate(request["decision"]),
            engine_pin(),
        )


def test_prior_frozen_controller_rejects_new_binding(context):
    source = os.environ.get("ASHARE_SUCCESSOR_OLD_SOURCE")
    if not source:
        pytest.skip("Explicit prior frozen source required")
    c = context
    bind_day(c, "2026-10-16")
    before = files(c["service"].root)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys;from ashare_quant.paper.online import OnlineService;OnlineService(sys.argv[1])._load()",
            str(c["service"].root),
        ],
        env=dict(os.environ, PYTHONPATH=source),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "PAPER_RUNTIME_CHANGED" in result.stderr
    assert files(c["service"].root) == before


def test_holding_intent_cannot_reintroduce_cancelled_orders(context):
    from ashare_quant.paper.online_contracts import OnlineDecision

    c = context
    plan, _ = bind_day(c, "2026-10-16")
    ids = c["phases"](plan)
    c["clock"]["at"] = datetime.fromisoformat("2026-10-16T10:00:31+08:00")
    row = intraday.prepare(c["service"], c["reader"], plan, ids["admission"])
    damaged = copy.deepcopy(row["decision"])
    damaged["targets"]["600000.XSHG"] += 700
    _, config = c["service"]._load()
    before = files(c["service"].root)
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_HOLD_ONLY"):
        successor.manifest_gate(
            damaged["strategy_receipt"]["continuation"],
            config,
            OnlineDecision.model_validate(damaged),
        )
    assert files(c["service"].root) == before
