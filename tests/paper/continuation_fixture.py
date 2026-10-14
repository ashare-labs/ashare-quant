"""Synthetic clock/transport parent run with the frozen pre-extension runtime."""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest
from ashare_quant.paper import intraday
from ashare_quant.paper.online import OnlineService


def build(root, original_test, fault=None):
    spec = importlib.util.spec_from_file_location("original_setup", original_test)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    with pytest.MonkeyPatch.context() as mp:
        service, reader, plan, ids, clock = original.setup(
            root,
            mp,
            partial=True,
            volumes={"600000": 33400, "600036": 221700, "601318": 120400},
        )
        intraday.prepare(service, reader, plan, ids["admission"])
        clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
        intraday.deliver(service, reader, plan, ids["execution"])
        opened = service.advance(str(plan.day), "opening")
        at = "2026-10-14T17:45:00+08:00"
        daily = dict(
            signal_day="2026-10-14",
            signal_ready=True,
            revisions=[],
            completed_at=at,
            calendar=dict(
                next_open="2026-10-15",
                open_days=["2026-10-14", "2026-10-15", "2026-10-16"],
                source=dict(observed_at=at),
            ),
            securities={
                s: dict(
                    rows=[dict(open=p, high=p, low=p, close=p, volume=1000000)] * 120,
                    source=dict(observed_at=at),
                )
                for s, p in zip(plan.universe, ("10", "20", "30"), strict=True)
            },
        )
        cap = digest(daily)
        mp.setattr(
            type(reader),
            "read",
            lambda self, config, kind, capture: (
                daily if kind == "daily" and capture == cap else None
            ),
        )
        (root / "daily.json").write_text(json.dumps(daily))
        (root / "plan.json").write_text(plan.model_dump_json())
        clock["at"] = datetime.fromisoformat(at)
        before = service.path.read_bytes()
        (root / "before-close-state.json").write_bytes(before)
        if fault != "no_close":
            service.deliver_observed(reader, str(plan.day), "closing", cap)
            if fault:
                with pytest.raises(DomainError, match="exited -9"):
                    service.advance(str(plan.day), "closing", fault=fault)
                service = OnlineService(service.root)
                service.resume("controlled close fault fixture")
            closed = service.advance(str(plan.day), "closing")
            assert closed["native"]["trades"] == opened["native"]["trades"]
            assert closed["native"]["settlement_count"] == 1
            assert (
                closed["native"]["account"]["total_cash"]
                == opened["native"]["account"]["total_cash"]
            )
            state = service.path.read_bytes()
            checkpoint = (service.root / str(plan.day) / "closing.chk").read_bytes()
            service = OnlineService(service.root)
            service.deliver_observed(reader, str(plan.day), "closing", cap)
            assert service.advance(str(plan.day), "closing") == closed
            assert service.path.read_bytes() == state
            assert (service.root / str(plan.day) / "closing.chk").read_bytes() == checkpoint
        else:
            closed = None
        (root / "fixture-evidence.json").write_text(
            json.dumps(
                dict(
                    evidence_kind="controlled_clock_transport_native_fixture_only",
                    real_calendar_acceptance=False,
                    opened=opened,
                    closed=closed,
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
