import json
import os
from pathlib import Path

import pytest

from ashare_quant.round_one.queries import QueryFacade, QueryRefused


def facade_without_transport():
    return object.__new__(QueryFacade)


@pytest.mark.parametrize(
    "role,api,kwargs,code",
    [
        ("matching", "get_price", {}, "QUERY_ROLE_DENIED"),
        ("conditional_research", "get_all_securities", {}, "QUERY_METADATA_AUDIT_ONLY"),
        ("conditional_research", "get_security_info", {}, "QUERY_METADATA_AUDIT_ONLY"),
        (
            "audit",
            "get_price",
            {"require_historical_pit": True},
            "QUERY_HISTORICAL_PIT_NOT_CERTIFIED",
        ),
    ],
)
def test_capability_denial_precedes_any_owner_call(role, api, kwargs, code):
    facade = facade_without_transport()
    facade._call = lambda q: pytest.fail("disallowed data crossed transport")
    with pytest.raises(QueryRefused) as exc:
        facade.query(api, {}, as_of="2024-03-04T15:30:00+08:00", role=role, **kwargs)
    assert exc.value.code == code


def test_query_requires_timezone():
    with pytest.raises(QueryRefused, match="QUERY_TIMEZONE_REQUIRED"):
        facade_without_transport().query("get_price", {}, as_of="2024-03-04T15:30:00", role="audit")


@pytest.fixture
def owner_runtime():
    values = [os.environ.get(k) for k in ("AQ_QUERY_BINDING", "AQ_QUERY_OWNER_PYTHON")]
    if not all(values):
        pytest.skip("fixed external owner query delivery required")
    return values


def test_real_owner_guards_not_stored_exception_replay_and_session_continues(owner_runtime):
    facade = QueryFacade(*owner_runtime, "D6")
    params = dict(
        security="000001.XSHE",
        count=1,
        end_date="2024-03-01",
        fields=["close"],
        frequency="daily",
        fq=None,
        skip_paused=False,
    )
    at = "2024-03-04T10:00:00+08:00"
    for change, code in [
        ({"end_date": "2099-12-31"}, "JQ_FUTURE_QUERY"),
        ({"fields": ["never_captured_bad_field"]}, "JQ_QUERY_FIELD"),
    ]:
        with pytest.raises(QueryRefused) as exc:
            facade.query("get_price", {**params, **change}, as_of=at, role="conditional_research")
        assert exc.value.origin == "ashare-data" and exc.value.code == code
    value = facade.query("get_price", params, as_of=at, role="conditional_research")
    assert value["rows"] == [{"close": "10.49", "index": "2024-03-01 00:00:00"}]
    assert value["matching_eligible"] is False


def test_earlier_clock_refused_and_adjustment_anchors_stay_separate(owner_runtime):
    facade = QueryFacade(*owner_runtime, "D4")
    p = dict(
        end_date="2024-06-13",
        fields=["open", "high", "low", "close", "volume", "money", "factor"],
        fq="pre",
        frequency="daily",
        security="000001.XSHE",
        skip_paused=False,
        start_date="2024-06-12",
    )
    with pytest.raises(QueryRefused):
        facade.query("get_price", p, as_of="2024-06-13T15:29:59+08:00", role="conditional_research")
    left = facade.query(
        "get_price", p, as_of="2024-06-13T15:30:00+08:00", role="conditional_research"
    )
    right = facade.query(
        "get_price", p, as_of="2024-06-14T15:30:00+08:00", role="conditional_research"
    )
    assert left["query_id"] != right["query_id"]
    assert left["adjustment_anchor"] == "2024-06-13" and right["adjustment_anchor"] == "2024-06-14"
    assert left["rows"] != right["rows"]


def test_nan_empty_and_fill_are_not_conflated(owner_runtime):
    facade = QueryFacade(*owner_runtime, "D4")
    import ashare_quant.round_one.queries as package_queries

    inputs = json.loads(Path(package_queries.__file__).with_name("query-inputs.json").read_text())
    values = {}
    for op in inputs["cases"]["B28"]["operations"]:
        values[op["label"]] = facade.query(
            **{k: op[k] for k in ("api", "parameters", "as_of", "role")}
        )
    assert values["filled"]["rows"][0]["close"] == "24.43"
    assert values["nan_fill"]["rows"][0]["close"] == {"nonfinite": "nan"}
    assert values["skipped"]["rows"] == []
    assert values["skipped"]["result"]["status"] == "observed_empty"
