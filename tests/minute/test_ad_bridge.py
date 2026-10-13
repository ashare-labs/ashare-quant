import copy
import json
from decimal import Context, localcontext

from ashare_data import Store

from ashare_quant.minute.inputs import export_ad_snapshot


def test_public_ad_snapshot_is_consumed_without_another_data_source(minute_doc, tmp_path):
    doc = copy.deepcopy(minute_doc)
    source = {
        "id": "minute-engineering-fixture",
        "kind": "synthetic",
        "location": "generated:minute-engineering-fixture",
        "label": "end",
        "observed_at": "2024-03-07T09:00:00+08:00",
        "time_semantics": "verified",
        "time_evidence": "Synthetic construction only",
        "volume_unit": "shares",
        "amount_unit": "CNY",
        "price_unit": "CNY",
        "price_basis": "unadjusted",
        "unit_evidence": "Synthetic shares and yuan",
    }
    bundle = {"schema_version": 1, "source": source, "calendar": [], "bars": []}
    requirements = []
    for day in doc["days"]:
        bundle["calendar"].append(
            {
                "date": day,
                "is_open": True,
                "sessions": doc["sessions"][day],
                "evidence": "synthetic declared sessions",
                "available_at": "2024-03-01T00:00:00+08:00",
            }
        )
        for start, end in doc["sessions"][day]:
            requirements.append({"symbols": doc["securities"], "start": start, "end": end})
    for row in doc["minute"]:
        bundle["bars"].append(
            {
                "symbol": row["security"],
                "timestamp": row["bar_end"],
                **{
                    k: row[k]
                    for k in ("open", "high", "low", "close", "volume", "amount", "available_at")
                },
                "quality": "synthetic",
                "quality_reason": "engineering only",
                "visibility_evidence": "synthetic visibility clock",
            }
        )
    # Other native tests import RQAlpha's stock API, which sets global Decimal
    # precision to 10. AD production is a separate runtime; use its standard
    # decimal context here without changing the caller/native context.
    with localcontext(Context(prec=28)):
        store = Store.init(tmp_path / "store")
        batch = store.import_bundle(bundle)
        report = store.validate([batch], requirements=requirements, policy="synthetic")
        assert report["passed"]
        snapshot_id = store.publish(report["id"])
        doc["producer"] = {"name": "ashare-data", "snapshot_id": snapshot_id}
        doc.pop("minute")
        with store.snapshot(snapshot_id) as snapshot:
            result = export_ad_snapshot(snapshot, doc, tmp_path / "export.json")
    assert len(result.bars) == 2160 and not result.document["real_market_data"]
    exported = json.loads((tmp_path / "export.json").read_text())
    assert exported["minute"][0]["evidence"]["snapshot_id"] == snapshot_id
    assert result.preflight()["ready"]
