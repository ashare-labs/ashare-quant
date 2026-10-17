"""One malformed observation per rule; must fail before opening a native book."""

import json
import runpy
from pathlib import Path

import pytest

from ashare_quant.portfolio.inputs import Inputs, digest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def source(tmp_path):
    manifest = json.loads((REPO / "examples/portfolio/manifest.json").read_text())
    document = runpy.run_path(str(REPO / "scripts/verify_portfolio.py"))["generate"](
        manifest, True
    )
    return tmp_path / "input.json", manifest, document


def load(source, **kwargs):
    path, manifest, document = source
    path.write_text(json.dumps(document))
    return Inputs(path, manifest, manifest["windows"][0], **kwargs)


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"high": "9", "low": "12"}, "INCONSISTENT_OHLC"),
        ({"open": "12"}, "INCONSISTENT_OHLC"),
        ({"close": "8"}, "INCONSISTENT_OHLC"),
        ({"lower_limit": "12", "upper_limit": "9"}, "INVERTED_PRICE_LIMITS"),
        ({"volume": "-1"}, "INVALID_DAILY_NUMBER"),
        ({"close": "NaN"}, "INVALID_DAILY_NUMBER"),
        ({"suspended": "false"}, "SUSPENSION_MUST_BE_BOOLEAN"),
    ],
)
def test_bad_row_rejected(source, changes, error):
    row = next(r for r in source[2]["daily"] if r["date"] == "2020-02-03")
    row.update(changes)
    with pytest.raises(ValueError, match=error):
        load(source)


def test_missing_state_and_unknown_actions_remain_blocked(source):
    next(r for r in source[2]["daily"] if r["date"] == "2020-02-03")["suspended"] = None
    source[2]["action_coverage"] = {"status": "unavailable"}
    report = load(source).preflight()
    assert not report["ready"]
    assert {r["field"] for r in report["gaps"]} >= {"suspended", "corporate_actions"}


def test_price_only_requires_named_model_and_opt_in(source):
    doc = source[2]
    doc.update(
        research_model="price_only_no_actions",
        economic_acceptance="price_only_diagnostic_not_economic_acceptance",
        state_mode="retrospective_model",
        action_coverage={"status": "unavailable"},
    )
    assert not load(source).preflight()["ready"]
    report = load(source, accept_price_only_research=True).preflight()
    assert report["ready"] and report["action_coverage"]["status"] == "unavailable"
    assert report["economic_acceptance"] == "price_only_diagnostic_not_economic_acceptance"
    doc["economic_acceptance"] = "conditional_research_only"
    with pytest.raises(ValueError, match="PRICE_ONLY_SCOPE"):
        load(source, accept_price_only_research=True)


def test_opt_in_does_not_waive_unlabeled_action_gap_or_price_gap(source):
    source[2]["action_coverage"] = {"status": "unavailable"}
    assert not load(source, accept_price_only_research=True).preflight()["ready"]
    source[2].update(
        research_model="price_only_no_actions",
        economic_acceptance="price_only_diagnostic_not_economic_acceptance",
        state_mode="retrospective_model",
    )
    next(r for r in source[2]["daily"] if r["date"] == "2020-02-03")["close"] = None
    assert not load(source, accept_price_only_research=True).preflight()["ready"]


def test_real_listing_cannot_claim_synthetic_coverage(source):
    doc = source[2]
    doc.update(
        real_market_data=True,
        producer={"name": "ashare-data"},
        action_coverage={"status": "source_covered"},
    )
    with pytest.raises(ValueError, match="SYNTHETIC_LISTING"):
        load(source)


def test_current_bar_not_in_history(source):
    baseline = digest(load(source).history("2020-02-03"))
    for r in source[2]["daily"]:
        if r["date"] >= "2020-02-03":
            r.update(open="20", high="21", low="19", close="20")
    assert digest(load(source).history("2020-02-03")) == baseline
