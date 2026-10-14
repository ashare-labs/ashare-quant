"""LC-1: owner replay must retain identity without trusting stored market fields."""

from datetime import datetime

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.online_adapter import execution_from_owner

from .test_late_close import opened_account


@pytest.mark.parametrize("committed", [False, True])
def test_same_capture_preserves_sealed_legacy_close(tmp_path, monkeypatch, committed):
    service, reader, row, _, _, capture, clock = opened_account(
        tmp_path, monkeypatch, "2026-10-14T17:45:00+08:00"
    )
    _, config = service._load()
    legacy = execution_from_owner(config, row, reader, "closing", capture).model_dump(mode="json")
    legacy["deadline"] = "2026-10-14T18:45:00+08:00"
    clock["at"] = datetime.fromisoformat("2026-10-14T17:46:00+08:00")
    service._deliver("closing", legacy)
    if committed:
        service.advance(row["day"], "closing")
    before = service.path.read_bytes()
    clock["at"] = datetime.fromisoformat("2026-10-15T07:00:00+08:00")
    assert service.deliver_observed(reader, row["day"], "closing", capture)["close"] == legacy
    assert service.path.read_bytes() == before
    closed = service.advance(row["day"], "closing")
    assert closed["native"]["settlement_count"] == 1


def test_same_capture_still_rereads_owner_and_compares_all_market_fields(tmp_path, monkeypatch):
    service, reader, row, _, daily, capture, clock = opened_account(
        tmp_path, monkeypatch, "2026-10-14T17:45:00+08:00"
    )
    _, config = service._load()
    legacy = execution_from_owner(config, row, reader, "closing", capture).model_dump(mode="json")
    legacy["deadline"] = "2026-10-14T18:45:00+08:00"
    clock["at"] = datetime.fromisoformat("2026-10-14T19:00:00+08:00")
    service._deliver("closing", legacy)
    before = service.path.read_bytes()
    daily["revisions"] = ["controlled revision"]
    with pytest.raises(DomainError, match="PAPER_OBSERVATION_REVISION"):
        service.deliver_observed(reader, row["day"], "closing", capture)
    daily["revisions"] = []
    daily["securities"]["600000.XSHG"]["rows"][-1]["volume"] += 1
    with pytest.raises(DomainError, match="PAPER_IDEMPOTENCY_CONFLICT"):
        service.deliver_observed(reader, row["day"], "closing", capture)
    assert service.path.read_bytes() == before
    fresh_row = {**row, "close": {**legacy, "data_manifest_sha256": "a" * 64}}
    fresh = execution_from_owner(config, fresh_row, reader, "closing", capture)
    assert "deadline" not in fresh.model_dump(mode="json")
