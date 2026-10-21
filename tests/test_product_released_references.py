"""Independent AQ checks reject mismatched released reference facts."""

import json

import pytest

from ashare_quant.portfolio.product_reference import owner_digest, source_bindings, validate
from ashare_quant.product.publication import consume
from tests.test_product_publication import protocol  # noqa: F401


@pytest.fixture
def released_document(protocol):  # noqa: F811
    release, query, config, refs, _, _, _ = protocol
    doc = json.loads(consume(release, query, config, refs).read_bytes())
    doc.pop("product_release")
    doc.pop("product_coverage")
    pin = dict(root="/synthetic/release", release_id=10, manifest_sha256="f" * 64)
    e = doc["reference_evidence"]
    e["reference_set"] = dict(name="formal", kind="market_release")
    e["source_bindings"] = source_bindings(dict(kind="market_release", release=pin))
    market = e["market"]
    market.update(kind="market_release", descriptor=dict(binding_sha256=pin["manifest_sha256"],
                  release=pin, historical_pit=False, execution_permission=False),
                  accepted_assumptions=dict(accept_daily_replay=True, accept_source_observed_limits=True))
    states = {}
    for row in market["rows"]:
        for field in ("suspended", "is_st", "reference_price", "upper_limit", "lower_limit"):
            row[field]["source_refs"] = [dict(manifest_sha256=pin["manifest_sha256"], release_id=10)]
        states[row["security"], row["date"]] = row
    market["admissions"] = [dict(security=r["security"], date=r["date"], side=side, issues=[],
        state_sha256=owner_digest(r)) for r in market["rows"] for side in ("BUY", "SELL")]
    for row in doc["daily"]:
        key = row["security"], row["date"]
        if key in states:
            row["state_evidence"]["reference_sha256"] = owner_digest(states[key])
    doc["owner_api_reads"] = [dict(operation="prices", receipt=dict(release=dict(pin)))]
    schedule = doc["corporate_schedule"]
    schedule["binding_sha256"] = schedule["coverage"]["binding_sha256"] = pin["manifest_sha256"]
    e["corporate"] = dict(binding_sha256=pin["manifest_sha256"], response_sha256=owner_digest(schedule))
    return doc


def test_released_reference_validates_without_reopening_live_files(released_document):
    validate(released_document)


@pytest.mark.parametrize("mutation", ["price", "fact", "corporate", "assumption", "pit", "source"])
def test_released_reference_cannot_be_rebound_or_promoted(released_document, mutation):
    doc = released_document
    market = doc["reference_evidence"]["market"]
    if mutation == "price":
        doc["owner_api_reads"][0]["receipt"]["release"]["release_id"] = 11
    elif mutation == "fact":
        market["rows"][0]["upper_limit"]["source_refs"][0]["release_id"] = 11
    elif mutation == "corporate":
        doc["reference_evidence"]["source_bindings"]["corporate"]["binding_sha256"] = "e" * 64
    elif mutation == "assumption":
        market["accepted_assumptions"] = {"accept_daily_replay": True, "accept_limit_model": True}
    elif mutation == "pit":
        market["descriptor"]["historical_pit"] = True
    else:
        market["rows"][0]["upper_limit"]["source_refs"] = []
    with pytest.raises(ValueError, match="PRODUCT_REFERENCE_CONFLICT"):
        validate(doc)
