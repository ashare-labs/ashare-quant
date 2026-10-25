"""Shared paper online fixtures; synthetic evidence only."""

from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_contracts import OnlineSession
from ashare_quant.storage.artifacts import digest
from tests.paper.fixtures import close, config, decision

MODE = {"evidence_kind": "calendar_fixture", "clock_basis": "fixture_clock"}

def setup(tmp_path):
    value = config(tmp_path.name).model_dump(mode="json")
    value.update(
        model="prior-close-standing-intent-observed-open.v1",
        data_mode="calendar_fixture",
        admission_reader_sha256=digest("explicit-test-owner"),
    )
    session = OnlineSession.model_validate(value)
    service = OnlineService(tmp_path)
    service.create(session)
    return service, session

def prepare(service, session, index=0, targets=(100, 100), parent=None):
    d = decision(session, index, targets, parent)
    d.update(MODE)
    return service.prepare_fixture(d)

def opening(row, prices=("10", "20")):
    day = row["day"]
    return {
        "trading_date": day,
        "intent_id": row["intent_id"],
        **MODE,
        "received_at": day + "T09:31:00+08:00",
        "rows": [
            {
                "security": s,
                "trading_date": day,
                "open": p,
                "observed_at": day + "T09:30:59+08:00",
                "source_as_of": day + "T09:30:58+08:00",
                "raw_sha256": digest([s, p]),
            }
            for s, p in zip(session_universe(row), prices, strict=True)
        ],
    }

def session_universe(row):
    return [r["security"] for r in row["decision"]["rows"]]

def closing(row):
    return {**close(row["day"], row["intent_id"]), **MODE}

def start(service, row, **kwargs):
    service.deliver_fixture("opening", opening(row))
    return service.advance(row["day"], "opening", **kwargs)

def finish(service, row, **kwargs):
    service.deliver_fixture("closing", closing(row))
    return service.advance(row["day"], "closing", **kwargs)
