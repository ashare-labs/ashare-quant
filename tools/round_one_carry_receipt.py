"""Pin and read AD's bounded carry facts and dev5 initialization, offline only.

Run with the separate AD dev5 interpreter using -I -B. This tool does not
authorize native execution or change any original case expectation.
"""

import importlib.metadata
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

DELIVERY = Path("/Users/yinming/Documents/Codex/2026-10-10/task-3")
CARRY_SHA = "b54b79d7b7df1f38bd8a1fc6d083e2ec3bb92d27f3e583a37ec2215808463a3e"
WHEEL_SHA = "7634b28baf3276b0c06479307e143fbf3237d4a17375812b87ac0b335ddadd3e"
QUERY_SHA = "2e44f60dc9e1569d1173ccaaf1adeb6a150fdc210320f4e71d254d5b02b3f361"
OWNER_COMMIT = "a6233015c3c36673085f386d49d5719c728d48f2"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    guard = module("receipt_guard", Path(__file__).with_name("round_one_tdx_receipt.py"))
    sys.addaudithook(guard.readonly)
    root = DELIVERY / "round-one-carry-delivery"
    binding_path = root / "carry-binding.json"
    if guard.sha(binding_path) != CARRY_SHA:
        raise RuntimeError("CARRY_BINDING_PIN_MISMATCH")
    binding = json.loads(binding_path.read_text())
    reader_path = root / binding["reader"]["path"]
    if guard.sha(reader_path) != binding["reader"]["sha256"]:
        raise RuntimeError("CARRY_READER_PIN_MISMATCH")
    reader = module("ad_public_carry", reader_path)
    options = dict(role="lifecycle_research", expected_binding_sha256=CARRY_SHA)
    results, calendars, guards = [], [], []
    for window, security in [
        ("carry-march", "000001.XSHE"),
        ("carry-march", "600000.XSHG"),
        ("B22-settlement", "000001.XSHE"),
        ("B26-known-fact-carry", "000001.XSHE"),
    ]:
        value = reader.read_carry(binding_path, window, security, **options)
        if value["all_requested_carry_prerequisites_certified"] is not False:
            raise RuntimeError("UNEXPECTED_CARRY_ADMISSION_CHANGE")
        results.append(value)
        for link in value["calendar"]["links"]:
            result = reader.read_calendar(
                binding_path, window, security, link["session_date"], **options
            )
            if any(result[k] != v for k, v in link.items()):
                raise RuntimeError("CARRY_CALENDAR_PUBLIC_READ_MISMATCH")
            calendars.append(dict(window_id=window, **result))
    for role in ("strategy", "matching"):
        try:
            reader.read_carry(binding_path, "carry-march", "000001.XSHE", role=role)
        except ValueError as exc:
            guards.append(dict(request=dict(role=role), error=str(exc)))
        else:
            raise RuntimeError("CARRY_ROLE_MUST_REFUSE")
    try:
        reader.read_calendar(
            binding_path, "B22-settlement", "000001.XSHE", "2024-03-07", **options
        )
    except ValueError as exc:
        guards.append(dict(request=dict(session_date="2024-03-07"), error=str(exc)))
    else:
        raise RuntimeError("CARRY_ENDPOINT_MUST_NOT_CLAMP")

    wheel_path = root / "dist/ashare_data-0.9.1.dev5-py3-none-any.whl"
    if guard.sha(wheel_path) != WHEEL_SHA:
        raise RuntimeError("DEV5_WHEEL_PIN_MISMATCH")
    if importlib.metadata.version("ashare-data") != "0.9.1.dev5":
        raise RuntimeError("DEV5_INSTALLED_VERSION_MISMATCH")
    site = Path(importlib.util.find_spec("ashare_data").origin).parent.parent
    files = {}
    with zipfile.ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.startswith("ashare_data/") and not name.endswith("/"):
                if (site / name).read_bytes() != archive.read(name):
                    raise RuntimeError("DEV5_INSTALLED_FILE_MISMATCH:" + name)
                files[name] = guard.sha(site / name)

    from ashare_data import Store

    query_path = DELIVERY / "round-one-data-delivery/query-products/binding.json"
    if guard.sha(query_path) != QUERY_SHA:
        raise RuntimeError("QUERY_BINDING_PIN_MISMATCH")
    query = json.loads(query_path.read_text())
    view = Store(query["store_path"]).jq_queries(query["groups"]["D4"]["dataset_id"])
    initialization = []
    for query_id, expected_status in [
        ("00e3e4b464a36b8a3f09d973de5a14e6b64da5560b1e9f4aa1e4be95ff1e4df0", "available"),
        ("11dddc4f1090a97f3d237758985548557d8435a3c1b77d9ee919662c9799ed9b", "unavailable"),
    ]:
        value = view.read_initialization(
            query_id, "000001.XSHE", valuation_date="2024-06-13",
            as_of="2024-06-14T09:00:00+08:00",
        ).to_dict()
        if value["status"] != expected_status or any(
            value[k] is not False
            for k in ("strategy_visible", "matching_eligible", "historical_pit", "current_quote")
        ):
            raise RuntimeError("INITIALIZATION_ENVELOPE_MISMATCH")
        if expected_status == "available":
            if value["rows"][0]["values"] != {"close": "10.8"}:
                raise RuntimeError("INITIALIZATION_CLOSE_MISMATCH")
            if value["observed_at"] != "2024-06-13T15:30:00+08:00":
                raise RuntimeError("INITIALIZATION_SOURCE_CLOCK_MISMATCH")
        elif value["rows"] or value["reason"] != "NOT_OBSERVED_AS_OF":
            raise RuntimeError("INITIALIZATION_FUTURE_READ_MUST_REFUSE")
        initialization.append(value)
    print(json.dumps(dict(
        schema="aq.bounded-carry-public-receipt.v1",
        status="PUBLIC_READS_VERIFIED_NOT_EXECUTION_ADMISSION",
        binding=str(binding_path), binding_sha256=CARRY_SHA,
        reader_sha256=binding["reader"]["sha256"],
        facts=binding["facts"], bound_evidence_files=len(binding["evidence"]),
        reads=results, calendar_reads=calendars, rejection_checks=guards,
        initialization=dict(
            owner_commit=OWNER_COMMIT, version="0.9.1.dev5",
            wheel=dict(path=str(wheel_path), sha256=WHEEL_SHA),
            installed_owner_files=files, query_binding=str(query_path),
            query_binding_sha256=guard.sha(query_path), reads=initialization,
        ),
        complete_carry_authorization=False, engine_run=False, network_used=False,
        source_covered_empty_is_not_exhaustive_absence=True,
        account_settlement_eligibility="AQ virtual model owns this; AD unknown is not a request for real-account certification",
        aq_remaining_data_blockers=[
            "exhaustive_other_event_absence", "non_delisting", "security_transformation",
            "600000.XSHG rights_issues coverage",
        ],
    ), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
