"""Real native ledger recovery from an explicit old-runtime synthetic fixture.

The source fixture is read-only. Only small copies under tmp_path are changed.
Future receipts below are controlled tests, never calendar execution evidence.
"""

import copy
import json
import os
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import calendar_successor as successor
from ashare_quant.paper import intraday, online
from ashare_quant.paper import service as runtime
from ashare_quant.paper import settled_migration as migration
from ashare_quant.paper.intraday_contracts import REBALANCE_MODEL, UNIVERSE
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_contracts import OnlineDecision
from ashare_quant.storage.artifacts import atomic, digest, lock, sha


def files(root):
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def context(tmp_path, monkeypatch):
    selected = os.environ.get("ASHARE_V3_MIGRATION_FIXTURE")
    if not selected:
        pytest.skip("Explicit old-runtime synthetic v3 fixture required")
    source = Path(selected)
    data = json.loads((source / "fixture.json").read_text())
    assert data["synthetic_replay_only"] and not data["actual_day_acceptance"]
    before = files(source)
    root = tmp_path / "copy"
    shutil.copytree(source / "account", root)
    service = OnlineService(root)
    state, _ = service._load(verify=False)
    assert state["engine_pin"] == data["engine_pin"]
    assert state["engine_pin"]["runtime"]["ashare-data"] == "0.8.0.dev2"
    assert runtime.engine_pin()["runtime"]["ashare-data"] == "0.10.0.dev5"
    clock = {"at": datetime.fromisoformat("2026-10-19T20:00:00+08:00")}
    for mod in (migration, successor, intraday, online):
        monkeypatch.setattr(mod, "now", lambda: clock["at"])
    args = dict(
        state_sha=sha(service.path.read_bytes()),
        from_sha=digest(state["engine_pin"]),
        to_sha=digest(runtime.engine_pin()),
        review_sha=digest("controlled compatibility review"),
        reason="Explicit isolated v3 fixture compatibility validation",
    )
    c = dict(source=source, data=data, service=service, original=state, args=args, clock=clock)
    yield c
    assert files(source) == before


def apply(c, **kwargs):
    return migration.migrate(c["service"], **c["args"], apply=True, **kwargs)


def test_migrate_preserves_native_financial_history_and_duplicate_recovery(context):
    c = context
    s = c["service"]
    before = files(s.root)
    old = c["original"]
    check = migration.migrate(s, **c["args"])
    assert not check["applied"] and files(s.root) == before
    with pytest.raises(DomainError, match="PAPER_RUNTIME_CHANGED"):
        s._load()
    applied = apply(c)
    state, _ = s._load()
    assert applied["applied"] and not applied["permits_new_day"]
    for key in old:
        assert state[key] == old[key] if key != "events" else state[key][:-1] == old[key]
    assert all(files(s.root)[p] == h for p, h in before.items() if p != "state.json")
    closed = s.report("2026-10-19", "closing")
    assert closed == c["data"]["closing"]
    assert closed["native"]["genesis"]["prior_trades_claimed"] is False
    trades = closed["native"]["trades"]
    assert [(t["side"], t["quantity"], t["price"]) for t in trades] == [
        ("SELL", 300, "9.99"),
        ("SELL", 100, "19.99"),
        ("BUY", 100, "30.01"),
    ]
    expected = Decimal(30000)
    for t in trades:
        value = Decimal(t["price"]) * t["quantity"]
        assert Decimal(t["commission"]) == max(Decimal(5), value * Decimal(".0003"))
        expected += value * (1 if t["side"] == "SELL" else -1) - Decimal(t["cost"])
    assert abs(Decimal(closed["native"]["account"]["total_cash"]) - expected) < Decimal("1e-8")
    after = files(s.root)
    assert apply(c)["unchanged"]
    for phase in ("opening", "closing"):
        assert s.advance("2026-10-19", phase) == c["data"][phase]
    assert s.resume("nothing pending") == state
    assert s.status()["runtime_matches"] and not s.status()["origin_runtime_matches"]
    assert files(s.root) == after


@pytest.mark.parametrize("point", ["origin_saved", "authorization_saved"])
def test_interrupted_migration_retries_once_without_reposting(context, point):
    def stop(stage):
        if stage == point:
            raise RuntimeError("controlled migration interruption")

    with pytest.raises(RuntimeError, match="controlled migration interruption"):
        apply(context, stage=stop)
    apply(context)
    state, _ = context["service"]._load()
    assert sum(e["kind"] == "settled_runtime_authorized" for e in state["events"]) == 1
    assert context["service"].report("2026-10-19", "closing") == context["data"]["closing"]
    before = files(context["service"].root)
    assert apply(context)["unchanged"] and files(context["service"].root) == before


@pytest.mark.parametrize("damage", ["state", "from", "to", "review", "unsettled", "checkpoint"])
def test_bad_selection_never_rewrites_account(context, damage):
    c = context
    s = c["service"]
    if damage in {"state", "from", "to"}:
        c["args"][damage + "_sha"] = "0" * 64
    elif damage == "review":
        c["args"]["review_sha"] = None
    elif damage == "unsettled":
        state = copy.deepcopy(c["original"])
        state["days"]["2026-10-19"]["status"] = "awaiting_close"
        atomic(s.path, state)
        c["args"]["state_sha"] = sha(s.path.read_bytes())
    else:
        with (s.root / "2026-10-19/closing.chk").open("ab") as f:
            f.write(b"changed")
    before = files(s.root)
    with pytest.raises(DomainError):
        apply(c)
    assert files(s.root) == before


@pytest.mark.parametrize("key", ["python", "rqalpha", "jsonpickle", "ashare-data", "rq"])
def test_unreviewed_runtime_changes_are_rejected(context, monkeypatch, key):
    pin = runtime.engine_pin()
    if key == "rq":
        pin[key] = "0" * 64
    else:
        pin["runtime"][key] = "999.0.0"
    monkeypatch.setattr(runtime, "engine_pin", lambda: pin)
    context["args"]["to_sha"] = digest(pin)
    before = files(context["service"].root)
    with pytest.raises(DomainError):
        apply(context)
    assert files(context["service"].root) == before


def test_existing_writer_lock_prevents_migration(context):
    s = context["service"]
    before = files(s.root)
    with lock(s.root / "writer.lock"), pytest.raises(DomainError, match="existing writer"):
        apply(context)
    assert files(s.root) == before


def test_migration_does_not_authorize_or_prepare_a_new_day(context):
    c = context
    apply(c)
    s = c["service"]
    decision = OnlineDecision.model_validate(c["original"]["days"]["2026-10-19"]["decision"])
    decision = decision.model_copy(update={"trading_date": date(2026, 10, 20)})
    before = files(s.root)
    with pytest.raises(DomainError, match="PAPER_MIGRATED_ACCOUNT_BINDING_REQUIRED"):
        s._prepare(decision)
    assert files(s.root) == before


@pytest.mark.parametrize("damage", ["authorization", "origin", "checkpoint", "identity", "owner"])
def test_changed_authorization_or_history_cannot_restore(context, damage):
    c = context
    apply(c)
    s = c["service"]
    state = json.loads(s.path.read_bytes())
    if damage in {"authorization", "owner", "identity"}:
        if damage == "authorization":
            state[migration.FIELD]["to_runtime_pin"]["sources"] = "0" * 64
        elif damage == "owner":
            state[migration.FIELD]["owner_reader_sha256"] = "0" * 64
        else:
            state["identity"] = "0" * 64
        atomic(s.path, state)
    else:
        target = (
            s.root / "runtime-origins" / (c["args"]["state_sha"] + ".json")
            if damage == "origin"
            else s.root / "2026-10-19/closing.chk"
        )
        with target.open("ab") as stream:
            stream.write(b"changed")
    before = files(s.root)
    with pytest.raises(DomainError):
        s.advance("2026-10-19", "closing")
    assert files(s.root) == before


@pytest.mark.parametrize("point", ["origin_saved", "binding_saved"])
def test_v3_successor_binding_is_recoverable_and_keeps_one_event(context, point):
    c = context
    apply(c)
    s = c["service"]
    reader, _ = successor_reader(c)
    plan = successor.build_plan(s, reader, "2026-10-20T09:35:00+08:00")
    state, _ = s._load()
    args = dict(
        state_sha=sha(s.path.read_bytes()),
        from_runtime_sha=digest(successor.current_authority(state)),
        runtime_sha=digest(runtime.engine_pin()),
        review_sha=digest("controlled review"),
    )

    def stop(stage):
        if stage == point:
            raise RuntimeError("controlled binding interruption")

    with pytest.raises(RuntimeError, match="controlled binding interruption"):
        successor.bind(s, reader, plan, **args, stage=stop)
    successor.bind(s, reader, plan, **args)
    state, _ = s._load()
    assert sum(e["kind"] == "holding_successor_bound" for e in state["events"]) == 1
    assert state["days"] == c["original"]["days"]
    assert state["identity"] == c["original"]["identity"]


def successor_reader(c):
    old = c["data"]
    state, _ = c["service"]._load()
    owner = state[migration.FIELD]["owner_reader_sha256"]
    baseline = digest("controlled next-day review")

    class Reader:
        packets = copy.deepcopy(old["packets"])

        def reviewed_successor(self):
            return dict(
                first_window=["2026-10-16", "2026-10-21"],
                admission_reader_sha256=owner,
                action_baseline_sha256=baseline,
                reviewed_at="2026-10-19T19:00:00+08:00",
                baseline_lineage=[old["plan"]["action_baseline_sha256"], baseline],
            )

        def read(self, config, kind, capture):
            assert config.admission_reader_sha256 == owner
            return copy.deepcopy(self.packets[capture])

        def intraday(self, config, capture):
            return self.read(config, "intraday", capture)

    reader = Reader()
    ids = {}
    for kind in ("admission", "execution"):
        packet = copy.deepcopy(old["packets"][old["ids"][kind]])
        for key in ("day", "completed_at", "not_before"):
            packet[key] = packet[key].replace("2026-10-19", "2026-10-20")
        for row in packet["assessment"]["rows"]:
            scope = row["action_scope"]
            scope["through_day"] = "2026-10-21"
            proof = scope["price_share_code_review"]
            proof.update(day="2026-10-20", baseline_sha256=baseline)
            proof["source_sha256"] = digest(
                {k: v for k, v in proof.items() if k not in {"status", "source_sha256"}}
            )
            if "execution" in row:
                for key in ("minute_end", "observed_at"):
                    row["execution"][key] = row["execution"][key].replace(
                        "2026-10-19", "2026-10-20"
                    )
        ids[kind] = digest(packet)
        reader.packets[ids[kind]] = packet
    close = copy.deepcopy(old["packets"][old["close_capture"]])
    close.update(signal_day="2026-10-20", completed_at="2026-10-20T15:00:40+08:00")
    close["calendar"].update(
        next_open="2026-10-21", open_days=["2026-10-20", "2026-10-21", "2026-10-22"]
    )
    for row in close["securities"].values():
        row["source"]["observed_at"] = "2026-10-20T15:00:20+08:00"
    ids["close"] = digest(close)
    reader.packets[ids["close"]] = close
    return reader, ids


@pytest.mark.parametrize("change", ["targets", "model", "owner_window"])
def test_changed_model_targets_or_unreviewed_day_cannot_bind(context, change):
    c = context
    apply(c)
    s = c["service"]
    reader, _ = successor_reader(c)
    plan = successor.build_plan(s, reader, "2026-10-20T09:35:00+08:00")
    value = plan.model_dump(mode="json")
    if change == "targets":
        value["target_quantities"][UNIVERSE[2]] += 100
    elif change == "model":
        value.update(
            model="prior-close-intent-minute-partial-cancel.v2",
            schema_version="paper-intraday-plan.v2",
            strategy_id="equal-weight",
        )
        value.pop("initial_inventory")
        value.pop("target_quantities")
    else:
        owned = reader.reviewed_successor()
        owned["first_window"][1] = "2026-10-19"
        reader.reviewed_successor = lambda: owned
    state, _ = s._load()
    before = files(s.root)
    with pytest.raises(DomainError):
        successor.bind(
            s,
            reader,
            value,
            state_sha=sha(s.path.read_bytes()),
            from_runtime_sha=digest(successor.current_authority(state)),
            runtime_sha=digest(runtime.engine_pin()),
            review_sha=digest("controlled refusal review"),
        )
    assert files(s.root) == before


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "open:after_opening_checkpoint",
        "close:inside_native_settlement",
        "close:after_complete_checkpoint",
    ],
)
def test_v3_holding_successor_uses_actual_native_positions_without_genesis_or_orders(
    context, fault
):
    c = context
    apply(c)
    s = c["service"]
    reader, ids = successor_reader(c)
    plan = successor.build_plan(s, reader, "2026-10-20T09:35:00+08:00")
    state, _ = s._load()
    args = dict(
        state_sha=sha(s.path.read_bytes()),
        from_runtime_sha=digest(successor.current_authority(state)),
        runtime_sha=digest(runtime.engine_pin()),
        review_sha=digest("controlled hold-only review"),
    )
    successor.bind(s, reader, plan, **args)
    stable = files(s.root)
    successor.bind(s, reader, plan, **args)
    assert files(s.root) == stable
    state, config = s._load()
    assert config.model == REBALANCE_MODEL
    assert state["identity"] == c["original"]["identity"]
    assert state["calendar_successor"]["schema_version"] == successor.V3_SCHEMA
    # Actual partial fill (100), not the original target (200).
    assert state["calendar_successor"]["hold_targets"][UNIVERSE[2]] == 100
    c["clock"]["at"] = datetime.fromisoformat("2026-10-20T09:35:31+08:00")
    intraday.prepare(s, reader, plan, ids["admission"])
    c["clock"]["at"] = datetime.fromisoformat("2026-10-20T09:38:31+08:00")
    intraday.deliver(s, reader, plan, ids["execution"])
    if fault and fault.startswith("open:"):
        with pytest.raises(DomainError, match="exited -9"):
            s.advance("2026-10-20", "opening", fault=fault.split(":", 1)[1])
        s.resume("controlled recovery")
    opened = s.advance("2026-10-20", "opening")
    n = opened["native"]
    previous = c["data"]["closing"]["native"]["account"]
    assert n["orders"] == {} and n["trades"] == [] and "genesis" not in n
    assert n["account"]["total_cash"] == previous["total_cash"]
    assert {k: v["quantity"] for k, v in n["account"]["positions"].items()} == {
        k: v["quantity"] for k, v in previous["positions"].items()
    }
    assert all(p["sellable"] == p["quantity"] for p in n["account"]["positions"].values())
    c["clock"]["at"] = datetime.fromisoformat("2026-10-20T15:00:41+08:00")
    s.deliver_observed(reader, "2026-10-20", "closing", ids["close"])
    if fault and fault.startswith("close:"):
        with pytest.raises(DomainError, match="exited -9"):
            s.advance("2026-10-20", "closing", fault=fault.split(":", 1)[1])
        s.resume("controlled settlement recovery")
    closed = s.advance("2026-10-20", "closing")
    assert closed["native"]["settlement_count"] == 2 and closed["native"]["cursor"] == 8
    assert closed["native"]["trades"] == [] and closed["native"]["orders"] == {}
    before = files(s.root)
    assert s.advance("2026-10-20", "opening") == opened
    assert s.advance("2026-10-20", "closing") == closed
    assert files(s.root) == before
    assert s.report("2026-10-19", "closing") == c["data"]["closing"]
