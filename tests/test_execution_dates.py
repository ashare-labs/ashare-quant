"""Public date syntax stays exact; no timestamp truncation or start-date authority."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from tests.support.daily.contracts import JobCreate

BASE = dict(
    window_id="w1",
    name="Prefix",
    idempotency_key="prefix",
    data_binding_sha256="a" * 64,
    assumption_ack_sha256="b" * 64,
)


@pytest.mark.parametrize(
    "value", ["2020-01-06T00:00:00+08:00", "20200106", 1578268800, True, datetime(2020, 1, 6)]
)
def test_end_date_does_not_coerce_timestamps(value):
    with pytest.raises(ValidationError):
        JobCreate(**BASE, end_date=value)


def test_arbitrary_start_and_skipped_date_lists_are_not_part_of_contract():
    for extra in [{"start_date": "2020-01-06"}, {"trading_dates": ["2020-01-03", "2020-01-07"]}]:
        with pytest.raises(ValidationError):
            JobCreate(**BASE, **extra)
    assert JobCreate(**BASE).end_date is None
    assert JobCreate(**BASE, end_date="2020-01-06").end_date.isoformat() == "2020-01-06"
