"""A frozen native parent, explicit runtime binding and a no-order successor.

All source responses and wall clocks here are controlled. Never actual cross-day
acceptance; the production account is not passed to these tests.
"""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday, online
from ashare_quant.paper import intraday_continuation as ext
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.service import engine_pin
from ashare_quant.storage.artifacts import digest, sha


def parent(tmp_path, fault=None):
    if not os.getenv("ASHARE_CONTINUATION_ORIGIN"):
        pytest.skip("Explicit isolated continuation origin required; not executed")
    origin = Path(os.environ["ASHARE_CONTINUATION_ORIGIN"])
    args = [
        sys.executable,
        "-B",
        str(Path(__file__).with_name("continuation_fixture.py")),
        str(tmp_path),
        str(origin / "tests/paper/test_intraday.py"),
    ]
    if fault:
        args.append(fault)
    r = subprocess.run(
        args,
        env=dict(os.environ, PYTHONPATH=str(origin / "src")),
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    daily = json.loads((tmp_path / "daily.json").read_text())
    original = IntradayPlan.model_validate_json((tmp_path / "plan.json").read_text())
    plan = IntradayPlan.model_validate(
        {
            **original.model_dump(mode="json"),
            "day": "2026-10-15",
            "start_at": "2026-10-15T10:00:00+08:00",
            "intent_deadline": "2026-10-15T10:05:00+08:00",
            "execution_deadline": "2026-10-15T10:10:00+08:00",
            "daily_capture": digest(daily),
        }
    )

    class Reader(intraday.IntradayReader):
        def read(self, config, kind, capture):
            assert kind == "daily" and capture == digest(daily)
            return copy.deepcopy(daily)

        def intraday(self, config, capture):
            # Read with the real immutable owner parser at an explicit fixture clock.
            assert self.pin() == config.admission_reader_sha256
            boot = "import sys,json;from datetime import datetime;sys.path.insert(0,sys.argv[1]);import ashare_data.observations.intraday as o;o.now=lambda:datetime.fromisoformat('2026-10-15T10:03:31+08:00');print(json.dumps(o.read(sys.argv[2],sys.argv[3])))"
            r = subprocess.run(
                [
                    self.python,
                    "-I",
                    "-B",
                    "-c",
                    boot,
                    str(self.source),
                    str(self.phase_store),
                    capture,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert r.returncode == 0, r.stderr
            return json.loads(r.stdout)

    reader = Reader(
        os.environ["ASHARE_CALENDAR_AD_PYTHON"],
        Path(os.environ["ASHARE_CALENDAR_AD_SOURCE"]),
        tmp_path / "ad",
        tmp_path / "next-phases",
    )
    return OnlineService(tmp_path / "native"), reader, plan, daily


def future_packets(reader):
    boot = r"""
import sys,json
from pathlib import Path
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1]).parent/'tests')]
import pytest
import ashare_data.observations.intraday as obs
from test_intraday_observation import sources
out={}
for kind,at,after in [('admission','10:00:30','10:00:00'),('execution','10:03:30','10:00:31')]:
 with pytest.MonkeyPatch.context() as mp:
  sources(mp,at='2026-10-15T'+at+'+08:00')
  original=obs.phase.fetch
  def fetch(url,referer):
   if '/security/ann?' in url:
    return original(url.replace('end_time=2026-10-15','end_time=2026-10-14'),referer)
   raw=original(url,referer)
   if 'yunhq' in url or 'hq.sinajs' in url or 'getKLineData' in url:
    raw=raw.replace(b'2026-10-14',b'2026-10-15').replace(b'20261014',b'20261015')
   return raw
  mp.setattr(obs.phase,'fetch',fetch)
  a=obs.capture(sys.argv[2],kind,'2026-10-15','2026-10-15T'+after+'+08:00')
  assert a['status']=='observed',a.get('blockers')
  p=obs.read(sys.argv[2],a['capture_id']);assert p['assessment']['ready'],p['assessment']['blockers']
  out[kind]=a['capture_id']
print(json.dumps(out))
"""
    r = subprocess.run(
        [reader.python, "-I", "-B", "-c", boot, str(reader.source), str(reader.phase_store)],
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def bind(service, reader, plan, monkeypatch):
    monkeypatch.setattr(ext, "now", lambda: datetime.fromisoformat("2026-10-14T18:00:00+08:00"))
    return ext.bind(
        service,
        reader,
        plan,
        sha(service.path.read_bytes()),
        digest(engine_pin()),
        digest(json.loads(service.path.read_text())["engine_pin"]),
    )


@pytest.mark.parametrize("fault", [None, "inside_native_settlement", "after_complete_checkpoint"])
def test_frozen_v2_close_once_preserves_partial_fills(tmp_path, fault):
    service, _, _, _ = parent(tmp_path, fault)
    evidence = json.loads((tmp_path / "fixture-evidence.json").read_text())
    opened, closed = (evidence[x]["native"] for x in ("opened", "closed"))
    assert closed["trades"] == opened["trades"]
    assert closed["account"]["positions"]["600000.XSHG"]["quantity"] == 300
    assert closed["account"]["open_orders"] == []
    assert closed["settlement_count"] == 1


@pytest.mark.parametrize("fault", [None, "event_committed:1", "after_opening_checkpoint"])
def test_same_identity_full_native_parent_next_day_holds_and_unlocks_t1(
    tmp_path, monkeypatch, fault
):
    service, reader, plan, _ = parent(tmp_path)
    old = json.loads(service.path.read_text())
    original_files = {
        p: p.read_bytes() for p in (service.root / "2026-10-14").iterdir() if p.is_file()
    }
    with pytest.raises(DomainError, match="PAPER_RUNTIME_CHANGED"):
        service._load()
    state = bind(service, reader, plan, monkeypatch)
    assert (state["identity"], state["head"], state["config"], state["engine_pin"]) == (
        old["identity"],
        old["head"],
        old["config"],
        old["engine_pin"],
    )
    bound = service.path.read_bytes()
    ext.bind(
        service,
        reader,
        plan,
        sha((service.root / "continuation-origin.json").read_bytes()),
        digest(engine_pin()),
        digest(old["engine_pin"]),
    )
    assert service.path.read_bytes() == bound
    ids = future_packets(reader)
    clock = {"at": datetime.fromisoformat("2026-10-15T10:00:31+08:00")}
    monkeypatch.setattr(intraday, "now", lambda: clock["at"])
    monkeypatch.setattr(online, "now", lambda: clock["at"])
    row = intraday.prepare(service, reader, plan, ids["admission"])
    assert row["decision"]["targets"] == state["continuation"]["hold_targets"]
    assert row["decision"]["targets"]["600000.XSHG"] == 300
    assert row["decision"]["strategy_receipt"]["scheduled"] is False
    clock["at"] = datetime.fromisoformat("2026-10-15T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    if fault:
        with pytest.raises(DomainError, match="exited -9"):
            service.advance(str(plan.day), "opening", fault=fault)
        service = OnlineService(service.root)
        service.resume("controlled successor checkpoint interruption")
    report = service.advance(str(plan.day), "opening")
    n = report["native"]
    assert n["parent_checkpoint"] == old["head"] and n["cursor"] == 6
    assert n["settlement_count"] == 1 and n["trades"] == [] and n["orders"] == {}
    assert all(p["sellable"] == p["quantity"] for p in n["account"]["positions"].values())
    assert (
        n["account"]["total_cash"]
        == json.loads((tmp_path / "fixture-evidence.json").read_text())["closed"]["native"][
            "account"
        ]["total_cash"]
    )
    raw = service.path.read_bytes()
    checkpoint = (service.root / str(plan.day) / "opening.chk").read_bytes()
    service = OnlineService(service.root)
    intraday.deliver(service, reader, plan, ids["execution"])
    assert service.advance(str(plan.day), "opening") == report
    assert (
        service.path.read_bytes() == raw
        and (service.root / str(plan.day) / "opening.chk").read_bytes() == checkpoint
    )
    daily15 = json.loads(
        json.dumps(json.loads((tmp_path / "daily.json").read_text()))
        .replace("2026-10-15", "2026-10-16")
        .replace("2026-10-14", "2026-10-15")
    )
    daily15["calendar"]["open_days"] = ["2026-10-15", "2026-10-16", "2026-10-19"]
    cap15 = digest(daily15)
    monkeypatch.setattr(
        type(reader),
        "read",
        lambda self, config, kind, capture: (
            copy.deepcopy(daily15) if kind == "daily" and capture == cap15 else None
        ),
    )
    clock["at"] = datetime.fromisoformat("2026-10-15T17:45:01+08:00")
    service.deliver_observed(reader, str(plan.day), "closing", cap15)
    closed15 = service.advance(str(plan.day), "closing")
    assert closed15["native"]["settlement_count"] == 2
    assert closed15["native"]["cursor"] == 8 and closed15["native"]["trades"] == []
    assert closed15["native"]["account"]["total_cash"] == n["account"]["total_cash"]
    assert closed15["native"]["continuation_runtime"]["engine_pin_sha256"] == digest(engine_pin())
    assert all(p.read_bytes() == value for p, value in original_files.items())
    (tmp_path / "successor-evidence.json").write_text(
        json.dumps(
            dict(
                evidence_kind="controlled_fixture_only",
                real_calendar_acceptance=False,
                report=report,
                close=closed15,
            ),
            indent=2,
        )
    )


def test_unclosed_parent_cannot_bind(tmp_path, monkeypatch):
    service, reader, plan, _ = parent(tmp_path, "no_close")
    raw = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_CLOSE_REQUIRED"):
        bind(service, reader, plan, monkeypatch)
    assert service.path.read_bytes() == raw


@pytest.mark.parametrize(
    "damage",
    [
        "state_hash",
        "runtime_hash",
        "origin_runtime",
        "action_baseline",
        "capacity",
        "daily",
        "late",
    ],
)
def test_binding_rejects_changed_inputs_without_account_write(tmp_path, monkeypatch, damage):
    service, reader, plan, _ = parent(tmp_path)
    raw = service.path.read_bytes()
    value = plan.model_dump(mode="json")
    if damage in {"action_baseline", "daily"}:
        value["action_baseline_sha256" if damage == "action_baseline" else "daily_capture"] = (
            "a" * 64
        )
    if damage == "capacity":
        value["max_minute_participation"] = "0.5"
    monkeypatch.setattr(
        ext,
        "now",
        lambda: datetime.fromisoformat(
            "2026-10-15T10:01:00+08:00" if damage == "late" else "2026-10-14T18:00:00+08:00"
        ),
    )
    with pytest.raises((DomainError, AssertionError)):
        ext.bind(
            service,
            reader,
            value,
            "a" * 64 if damage == "state_hash" else sha(raw),
            "b" * 64 if damage == "runtime_hash" else digest(engine_pin()),
            "c" * 64 if damage == "origin_runtime" else digest(json.loads(raw)["engine_pin"]),
        )
    assert service.path.read_bytes() == raw


@pytest.mark.parametrize("damage", ["holding", "archive", "parent", "runtime"])
def test_bound_continuation_cannot_change_original_ledger_or_authorization(
    tmp_path, monkeypatch, damage
):
    service, reader, plan, _ = parent(tmp_path)
    bind(service, reader, plan, monkeypatch)
    original_day = {
        p: p.read_bytes() for p in (service.root / "2026-10-14").iterdir() if p.is_file()
    }
    state = json.loads(service.path.read_text())
    if damage == "holding":
        state["continuation"]["hold_targets"]["600000.XSHG"] += 700
    elif damage == "parent":
        state["continuation"]["parent_checkpoint"] = "e" * 64
    elif damage == "runtime":
        state["continuation"]["runtime_pin"]["sources"] = "d" * 64
    else:
        (service.root / "continuation-origin.json").write_text("{}")
    service.path.write_text(json.dumps(state))
    with pytest.raises(DomainError):
        service._load()
    assert all(p.read_bytes() == data for p, data in original_day.items())
    assert not (service.root / "2026-10-15").exists()


def test_cli_build_plan_reads_closed_checkpoint_without_binding_or_reinitializing(tmp_path):
    service, reader, _, _ = parent(tmp_path)
    state = service.path.read_bytes()
    output = tmp_path / "nextday-plan.json"
    args = [
        sys.executable,
        "-B",
        "-m",
        "ashare_quant.paper.continuation_cli",
        "--root",
        str(service.root),
        "--ad-source",
        str(reader.source),
        "--ad-python",
        reader.python,
        "--daily-store",
        str(reader.daily_store),
        "--phase-store",
        str(reader.phase_store),
        "--engine-pin-sha",
        digest(engine_pin()),
        "--origin-engine-pin-sha",
        digest(json.loads(state)["engine_pin"]),
        "build-plan",
        "--start-at",
        "2026-10-15T09:35:00+08:00",
        "--output",
        str(output),
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    value = json.loads(r.stdout)["result"]
    assert value["activated"] is False and value["account_unchanged"] is True
    assert value["origin_state_sha256"] == sha(state) and value["plan_sha256"] == sha(
        output.read_bytes()
    )
    assert json.loads(output.read_bytes())["execution_deadline"] == "2026-10-15T09:50:00+08:00"
    assert (
        service.path.read_bytes() == state
        and not (service.root / "continuation-origin.json").exists()
    )
