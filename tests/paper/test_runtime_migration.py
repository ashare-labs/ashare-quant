"""Frozen c5 bound fixture -> explicit authorization; no real account writes."""

import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday, online
from ashare_quant.paper import runtime_migration as migration
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_contracts import OnlineDecision
from ashare_quant.paper.service import engine_pin
from ashare_quant.storage.artifacts import canonical, digest, sha
from tests.paper.test_intraday_continuation import future_packets


@pytest.fixture(scope="module")
def frozen_bound(tmp_path_factory):
    if not os.getenv("ASHARE_MIGRATION_FROM"):
        pytest.skip("Explicit historical source fixture required for cross-version migration checks")
    root = tmp_path_factory.mktemp("c5-bound")
    source = Path(os.environ["ASHARE_MIGRATION_FROM"])
    boot = """
import sys,json
from pathlib import Path
import pytest
sys.path.insert(0,sys.argv[1])
from tests.paper.test_intraday_continuation import parent,bind
from ashare_quant.storage.artifacts import digest
from ashare_quant.paper.service import engine_pin
with pytest.MonkeyPatch.context() as mp:
 service,reader,plan,daily=parent(Path(sys.argv[2]))
 bind(service,reader,plan,mp)
 (Path(sys.argv[2])/'bound-plan.json').write_text(plan.model_dump_json())
 print(digest(engine_pin()))
"""
    run = subprocess.run(
        [sys.executable, "-B", "-c", boot, str(source), str(root)],
        env=dict(os.environ, PYTHONPATH=str(source / "src")),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "495f0a425a123b65a0704f0d473fff34f6facac46d5e4d914809583862adfdee"
    return root


def setup(tmp_path, monkeypatch, frozen_bound):
    root = tmp_path / "fixture"
    shutil.copytree(frozen_bound, root)
    service = OnlineService(root / "native")
    raw = service.path.read_bytes()
    state = json.loads(raw)
    plan = IntradayPlan.model_validate_json((root / "bound-plan.json").read_bytes())
    args = dict(
        state_sha=sha(raw),
        from_sha=digest(state["continuation"]["runtime_pin"]),
        to_sha=digest(engine_pin()),
        binding_sha=digest(state["continuation"]),
        review_sha=digest("explicit controlled fixture review only"),
        reason="controlled pre-start migration fixture",
    )
    clock = {"at": datetime.fromisoformat("2026-10-14T19:30:00+08:00")}
    monkeypatch.setattr(migration, "now", lambda: clock["at"])
    monkeypatch.setattr(online, "now", lambda: clock["at"])
    monkeypatch.setattr(intraday, "now", lambda: clock["at"])
    daily = json.loads((root / "daily.json").read_bytes())

    class Reader(intraday.IntradayReader):
        def read(self, config, kind, capture):
            assert kind == "daily" and capture == digest(daily)
            return copy.deepcopy(daily)

        def intraday(self, config, capture):
            boot = "import sys,json;from datetime import datetime;sys.path.insert(0,sys.argv[1]);import ashare_data.observations.intraday as o;o.now=lambda:datetime.fromisoformat('2026-10-15T10:03:31+08:00');print(json.dumps(o.read(sys.argv[2],sys.argv[3])))"
            run = subprocess.run(
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
            )
            assert run.returncode == 0, run.stderr
            return json.loads(run.stdout)

    reader = Reader(
        os.environ["ASHARE_CALENDAR_AD_PYTHON"],
        Path(os.environ["ASHARE_CALENDAR_AD_SOURCE"]),
        root / "ad",
        root / "next-phases",
    )
    return service, state, args, plan, reader, daily, clock


def test_explicit_migration_preserves_binding_rejects_c5_and_runs_native_successor(
    tmp_path, monkeypatch, frozen_bound
):
    service, old, args, plan, reader, daily, clock = setup(tmp_path, monkeypatch, frozen_bound)
    before = service.path.read_bytes()
    original_files = migration.original_files(service.root, "2026-10-14")
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_BINDING_CHANGED"):
        service._load()
    assert migration.migrate(service, **args)["applied"] is False
    assert service.path.read_bytes() == before
    assert not (service.root / "runtime-origin.json").exists()
    applied = migration.migrate(service, **args, apply=True)
    migrated, config = service._load()
    assert migrated["archived_continuation"] == old["continuation"]
    assert "continuation" not in migrated
    assert all(
        migrated[k] == old[k]
        for k in ("identity", "config", "engine_pin", "days", "head", "last_day")
    )
    assert migrated["events"][:-1] == old["events"]
    assert (service.root / "runtime-origin.json").read_bytes() == before
    migrated_bytes = service.path.read_bytes()
    assert migration.migrate(service, **args, apply=True)["unchanged"] is True
    assert service.path.read_bytes() == migrated_bytes
    source = Path(os.environ["ASHARE_MIGRATION_FROM"])
    boot = "import sys;from ashare_quant.paper.online import OnlineService;OnlineService(sys.argv[1])._load()"
    rejected = subprocess.run(
        [sys.executable, "-B", "-c", boot, str(service.root)],
        env=dict(os.environ, PYTHONPATH=str(source / "src")),
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0 and "PAPER_RUNTIME_CHANGED" in rejected.stderr
    # Legacy original-day capture is reread, not silently returned from state.
    service.deliver_observed(reader, "2026-10-14", "closing", digest(daily))
    assert service.path.read_bytes() == migrated_bytes
    ids = future_packets(reader)
    clock["at"] = datetime.fromisoformat("2026-10-15T10:00:31+08:00")
    row = intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-15T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    opened = service.advance(str(plan.day), "opening")
    assert opened["native"]["cursor"] == 6 and opened["native"]["settlement_count"] == 1
    assert opened["native"]["trades"] == [] and opened["native"]["orders"] == {}
    assert all(
        p["quantity"] == p["sellable"] for p in opened["native"]["account"]["positions"].values()
    )
    assert opened["native"]["continuation_runtime"]["manifest_sha256"] == args["binding_sha"]
    assert (
        opened["native"]["continuation_runtime"]["authorization_sha256"]
        == applied["authorization_sha256"]
    )
    request = json.loads((service.root / str(plan.day) / "opening-request.json").read_bytes())
    from ashare_quant.paper.intraday_continuation import verify_request

    for field in ("runtime_authorization", "runtime_authorization_event"):
        bad = copy.deepcopy(request)
        bad.pop(field)
        with pytest.raises(DomainError, match="PAPER_.*(AUTHORIZATION|RUNTIME)"):
            verify_request(
                bad, config, OnlineDecision.model_validate(row["decision"]), engine_pin()
            )
    daily.update(signal_day=str(plan.day), completed_at="2026-10-15T19:07:00+08:00")
    for item in daily["securities"].values():
        item["source"]["observed_at"] = daily["completed_at"]
    clock["at"] = datetime.fromisoformat("2026-10-15T19:08:00+08:00")
    service.deliver_observed(reader, str(plan.day), "closing", digest(daily))
    closed = service.advance(str(plan.day), "closing")
    assert closed["native"]["cursor"] == 8 and closed["native"]["settlement_count"] == 2
    assert closed["native"]["trades"] == [] and closed["native"]["orders"] == {}
    assert closed["native"]["account"]["total_cash"] == opened["native"]["account"]["total_cash"]
    assert migration.original_files(service.root, "2026-10-14") == original_files
    final = service.path.read_bytes()
    service.deliver_observed(reader, str(plan.day), "closing", digest(daily))
    assert service.advance(str(plan.day), "closing") == closed
    assert service.path.read_bytes() == final
    (tmp_path / "migration-proof.json").write_text(
        json.dumps(
            dict(
                evidence_kind="controlled_fixture_only",
                original_binding=old["continuation"],
                authorization=migrated["runtime_authorization"],
                original_files=original_files,
                opened=opened,
                closed=closed,
                old_c5_refused=True,
            ),
            indent=2,
        )
    )


@pytest.mark.parametrize("point", ["backup_saved", "authorization_saved"])
def test_interrupted_migration_retry_is_atomic(tmp_path, monkeypatch, frozen_bound, point):
    service, _, args, _, _, _, _ = setup(tmp_path, monkeypatch, frozen_bound)
    before = service.path.read_bytes()

    def interrupt(where):
        if where == point:
            raise RuntimeError("controlled migration interruption")

    with pytest.raises(RuntimeError, match="controlled migration interruption"):
        migration.migrate(service, **args, apply=True, stage=interrupt)
    if point == "backup_saved":
        assert service.path.read_bytes() == before
    migration.migrate(OnlineService(service.root), **args, apply=True)
    state, _ = service._load()
    assert len([e for e in state["events"] if e["kind"] == "execution_runtime_authorized"]) == 1
    assert (service.root / "runtime-origin.json").read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["manifest", "authorization", "event", "duplicate", "backup", "old_field", "native"]
)
def test_modified_authorization_fails_before_any_successor(
    tmp_path, monkeypatch, frozen_bound, damage
):
    service, _, args, _, _, _, _ = setup(tmp_path, monkeypatch, frozen_bound)
    migration.migrate(service, **args, apply=True)
    state = json.loads(service.path.read_bytes())
    if damage == "manifest":
        state["archived_continuation"]["bound_at"] = "2026-10-14T18:01:00+08:00"
    elif damage == "authorization":
        state["runtime_authorization"]["review_sha256"] = "a" * 64
    elif damage == "event":
        state["events"][-1]["details"]["authorization"] = "a" * 64
    elif damage == "duplicate":
        state["events"].append(copy.deepcopy(state["events"][-1]))
    elif damage == "backup":
        (service.root / "runtime-origin.json").write_bytes(b"{}")
    elif damage == "native":
        path = service.root / "2026-10-14" / "opening-report.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        state["continuation"] = state["archived_continuation"]
    service.path.write_bytes(canonical(state))
    with pytest.raises(DomainError, match="PAPER_.*(AUTHORIZATION|ORIGIN)"):
        service._load()
    assert not (service.root / "2026-10-15").exists()


@pytest.mark.parametrize(
    "damage", ["state_sha", "from_sha", "to_sha", "binding_sha", "started", "review"]
)
def test_bad_migration_selection_never_writes(tmp_path, monkeypatch, frozen_bound, damage):
    service, _, args, _, _, _, clock = setup(tmp_path, monkeypatch, frozen_bound)
    if damage.endswith("sha"):
        args[damage] = "a" * 64
    elif damage == "started":
        clock["at"] = datetime.fromisoformat("2026-10-15T10:00:00+08:00")
    else:
        args["review_sha"] = None
    before = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_"):
        migration.migrate(service, **args, apply=True)
    assert service.path.read_bytes() == before
    assert not (service.root / "runtime-origin.json").exists()


def test_migration_respects_existing_writer_lock(tmp_path, monkeypatch, frozen_bound):
    from ashare_quant.storage.artifacts import lock

    service, _, args, _, _, _, _ = setup(tmp_path, monkeypatch, frozen_bound)
    before = service.path.read_bytes()
    with lock(service.root / "writer.lock"):
        with pytest.raises(DomainError) as error:
            migration.migrate(service, **args, apply=True)
        assert error.value.code == "M2_JOB_WRITER_BUSY"
    assert service.path.read_bytes() == before
    assert not (service.root / "runtime-origin.json").exists()
