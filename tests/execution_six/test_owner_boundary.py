"""Owner consumer boundaries; optional local real reads never count as AQ runs."""

import os

import pytest

from ashare_quant.execution_six.contracts import CASES
from ashare_quant.execution_six.owner import PublicOwner, share_count


@pytest.mark.parametrize("value", [True, 100, "1.5", "NaN", "-1"])
def test_volume_conversion_refuses_unknown_or_nonintegral_shares(value):
    with pytest.raises(ValueError):
        share_count(value)


def test_exact_decimal_shares_remain_exact():
    assert share_count("12345678.0") == 12345678


def test_public_owner_does_not_fill_missing_clock_or_expose_capacity():
    binding, python = os.getenv("AQ_SIX_BINDING"), os.getenv("AQ_SIX_OWNER_PYTHON")
    if not binding or not python:
        pytest.skip("Optional fixed public owner inputs required; not a real-case run")
    owner = PublicOwner(binding, python, "B12")
    assert owner.read("auction", CASES["B12"].at("09:25"), consumer="strategy").value["status"] == "unavailable"
    later = owner.read("auction", CASES["B12"].at("09:28"), consumer="strategy").value
    assert later["status"] == "available"
    assert all(r["available_at"] <= later["as_of"] for r in later["rows"])
    with pytest.raises(RuntimeError, match="REQUIRED_OBSERVATION_UNAVAILABLE"):
        owner.read("bar", CASES["B12"].at("09:30"), consumer="engine")
    with pytest.raises(RuntimeError, match="ROLE_DENIED"):
        owner.read("capacity", CASES["B12"].at("10:00"), consumer="strategy")
    assert owner.bootstrap(CASES["B12"])["owner_bootstrap"]["unknown"]["previous_session"] is None
