"""Offline AQ projection of the fixed AD public TDX/evidence APIs.

Run in an export environment containing AD's newer wheel, separately from the
native engine's pinned owner runtime. This module performs no source collection.
"""

import argparse
import hashlib
import json
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta
from importlib.util import find_spec
from pathlib import Path

from .inputs import MinuteInputs, require
from .research import ASSUMPTIONS, CALENDAR, DAYS, IDS, PROFILE, SCHEMA, SESSIONS

AD_COMMIT = "171359e2b96aae6ea2fc3d45334fbb7484385c34"
AD_WHEEL_SHA = "24161f86a8e6689738e5e4a89e33fffad2949f687a3acb364b80504202de8349"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_ad_install(wheel):
    raw = Path(wheel).read_bytes()
    require(sha(raw) == AD_WHEEL_SHA, "MINUTE_EXPORT_AD_WHEEL_PIN")
    root = Path(next(iter(find_spec("ashare_data").submodule_search_locations)))
    with zipfile.ZipFile(wheel) as archive:
        sources = {
            n: archive.read(n)
            for n in archive.namelist()
            if n.startswith("ashare_data/") and n.endswith(".py")
        }
    actual = {
        "ashare_data/" + p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.py")
    }
    require(sources and sources == actual, "MINUTE_EXPORT_INSTALLED_AD_DIFFERS_FROM_WHEEL")


def project(captures, hashes, binding_path, binding_sha):
    # Imports are intentionally here: the execution environment still uses its
    # own older, source-pinned AD. Only this offline export needs the newer API.
    from ashare_data import MinuteEvidenceView, TDXBarView

    binding_path = Path(binding_path)
    raw_binding = binding_path.read_bytes()
    require(sha(raw_binding) == binding_sha, "MINUTE_EXPORT_BINDING_HASH")
    view = MinuteEvidenceView(binding_path, expected_binding_sha256=binding_sha)
    binding = json.loads(raw_binding)
    inventory = {item["path"]: item for item in binding["files"]}

    def bound_bytes(name):
        raw = (binding_path.parent / name).read_bytes()
        require(
            sha(raw) == inventory[name]["sha256"] and len(raw) == inventory[name]["bytes"],
            "MINUTE_EXPORT_BOUND_SOURCE_CHANGED",
        )
        return raw

    supplement = json.loads(bound_bytes("supplement/action-coverage-supplement.json"))
    # Read validated source bytes without extracting an archive or parsing a
    # fresh provider response. Acquisition and provider normalization remain AD-owned.
    import io

    tdx_actions = []
    with tarfile.open(fileobj=io.BytesIO(bound_bytes("supplement/acquisition.tar.gz"))) as tar:
        for entry in supplement["tdx"]:
            if entry["security"] in IDS:
                tdx_actions.append(
                    {
                        "security": entry["security"],
                        "report": entry,
                        "rows": json.load(tar.extractfile(entry["source_ref"])),
                    }
                )

    sessions = {
        d: [[d + "T" + a + ":00+08:00", d + "T" + b + ":00+08:00"] for a, b in SESSIONS]
        for d in DAYS
    }
    labels = []
    for pairs in sessions.values():
        for start, end in pairs:
            at = datetime.fromisoformat(start) + timedelta(minutes=1)
            while at <= datetime.fromisoformat(end):
                labels.append(at)
                at += timedelta(minutes=1)
    previous_labels = [d + "T15:00:00+08:00" for d in (CALENDAR[0], DAYS[0])]
    doc = dict(
        schema=SCHEMA,
        case_id=PROFILE,
        real_market_data=True,
        producer={
            "name": "ashare-data",
            "interfaces": ["TDXBarView", "MinuteEvidenceView"],
            "commit": AD_COMMIT,
            "wheel_sha256": AD_WHEEL_SHA,
        },
        adapter={"name": "ashare-quant", "module": "ashare_quant.minute.tdx_export"},
        source_refs={"tdx_manifest_sha256": hashes, "state_binding_sha256": binding_sha},
        limitations=list(ASSUMPTIONS)
        + [
            "Research admission does not change the original three-stock strict request or B27 gap.",
            "No JQ comparison. Source authenticity, full action absence and historical PIT unproven.",
            "Native ledger uses a 4% execution envelope, not certified exchange limits.",
            "Auction/opening labels omitted only in this new example; day_open API unavailable.",
            "Flat initial account and mandatory flat final account; no inherited dividend entitlement.",
            "Daily equity marks the last selected 14:56 label, not the official closing auction.",
        ],
        price_basis="raw",
        volume_unit="share",
        amount_unit="CNY",
        visibility="assumed_closed",
        state_visibility="posthoc_daily_replay",
        state_replay={
            "model": "dated-daily-state-0900.v1",
            "purpose": "retrospective_research",
            "evidence": {"binding_sha256": binding_sha},
            "limitations": [ASSUMPTIONS[2], ASSUMPTIONS[3]],
        },
        securities=list(IDS),
        calendar=CALENDAR,
        days=DAYS,
        sessions=sessions,
        states=[],
        minute=[],
        actions=[],
        action_coverage={
            "status": "bounded_sources_no_known_overlap",
            "complete": False,
            "absence_proved": False,
            "evidence": {"binding_sha256": binding_sha},
            "public_action_query": view.corporate_actions(
                list(IDS), start=CALENDAR[0], end=DAYS[-1]
            ),
            "tdx_actions": tdx_actions,
        },
        research={
            "profile": PROFILE,
            "purpose": "retrospective_research",
            "assumptions": list(ASSUMPTIONS),
            "strict_execution_admitted": False,
            "interval_verified": False,
            "price_limit_verified": False,
            "envelope": "raw-previous-close-4pct-inward-cent.v1",
            "original_admission": view.admission_report(),
            "tdx_reports": {},
            "calendars": view.calendars(),
        },
    )
    require(set(hashes) == set(IDS), "MINUTE_EXPORT_TWO_MANIFEST_PINS_REQUIRED")
    for sid in IDS:
        tdx = TDXBarView(Path(captures) / sid, expected_sha256=hashes[sid])
        read = tdx.get_price(
            mode="retrospective",
            allow_partial=True,
            labels=[t.isoformat() for t in labels] + previous_labels,
        )
        require(read.report["missing_requested_labels"] == [], "MINUTE_EXPORT_LABEL_GAP")
        # Directory location is not source evidence; retain all source claims,
        # hashes, limitations and clocks for path-independent offline reproduction.
        doc["research"]["tdx_reports"][sid] = {
            k: v for k, v in read.report.items() if k != "capture_directory"
        }
        for day, prev in zip(DAYS, previous_labels, strict=True):
            observed = view.state(
                sid, day, require_fields=["listed", "suspended", "is_st", "previous_close"]
            )
            doc["states"].append(
                {
                    "security": sid,
                    "date": day,
                    **{
                        k: observed[k]["value"]
                        for k in (
                            "listed",
                            "suspended",
                            "is_st",
                            "previous_close",
                            "reference_price",
                            "upper_limit",
                            "lower_limit",
                        )
                    },
                    "available_at": None,
                    "historical_available_at": None,
                    "observed_at": binding["received_at"],
                    "observed_at_scope": "source package receipt, not first provider publication",
                    "replay_effective_at": day + "T09:00:00+08:00",
                    "evidence": {"binding_sha256": binding_sha},
                    "source_observation": observed,
                    "tdx_previous_close": {
                        "time": prev,
                        "close": str(read.data.loc[prev, "close"]),
                        "manifest_sha256": hashes[sid],
                    },
                }
            )
        for at in labels:
            row = read.data.loc[at]
            doc["minute"].append(
                {
                    "security": sid,
                    **{k: str(row[k]) for k in ("open", "high", "low", "close", "volume")},
                    "amount": str(row["money"]),
                    "source_label": at.isoformat(),
                    "bar_start": None,
                    "bar_end": None,
                    "available_at": None,
                    "historical_available_at": None,
                    "replay_bar_start": (at - timedelta(minutes=1)).isoformat(),
                    "replay_bar_end": at.isoformat(),
                    "evidence": {"manifest_sha256": hashes[sid], "source": "tdx_native_0x052d"},
                }
            )
    return doc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ad-wheel", required=True)
    p.add_argument("--tdx-root", required=True)
    p.add_argument("--tdx-sha", action="append", required=True, metavar="SECURITY=SHA256")
    p.add_argument("--state-binding", required=True)
    p.add_argument("--state-sha", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    verify_ad_install(args.ad_wheel)
    pairs = [s.split("=", 1) for s in args.tdx_sha]
    require(len(pairs) == len(dict(pairs)), "MINUTE_EXPORT_DUPLICATE_MANIFEST_PIN")
    doc = project(args.tdx_root, dict(pairs), args.state_binding, args.state_sha)
    body = json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "input.json"
        path.write_bytes(body)
        MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)
    with Path(args.output).open("xb") as stream:
        stream.write(body)
    print(
        json.dumps(
            {
                "output": args.output,
                "sha256": sha(body),
                "rows": len(doc["minute"]),
                "profile": PROFILE,
                "strict_execution_admitted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
