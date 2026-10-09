"""Serve one committed evidence version; no execution, Store access or pickle decoding."""
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from importlib.resources import files

from pydantic import ValidationError

from . import __version__
from .contracts import SHANGHAI, DomainError
from .d1_result_contracts import (
    BR1ResultDetail,
    BR1ResultList,
    FrozenResult,
    ResultIndex,
    ServingIdentity,
)
from .identity import source_sha256

RESULT_ID = "br1-600000-20200103-20200106-f9c58f0-v1"
CATALOG_SHA256 = "12c88eb2130d17311f1cc1b8d874c4631dfbedfaf726609e8e51b0b705756839"
SNAPSHOT_SHA256 = "6469d8d69395fa33477f8ed069980e135b0c0a26716d67ec58b7b8631b6abc26"
CODE_COMMIT = "8979269e1adb393bcac52eba4094de136f5a5898"
EVIDENCE_COMMIT = "f9c58f0e09350a1b97234dc485fc7bc4e055a92f"
SERVING_SOURCE_SHA256 = source_sha256()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _bundle_root():
    return files("ashare_quant").joinpath("result_evidence/br1-f9-v1")


def _require(condition):
    if not condition:
        raise ValueError("Fixed result evidence is inconsistent")


def _verified_bundle():
    root = _bundle_root()
    checking_id = "catalog"
    try:
        raw_catalog = root.joinpath("catalog.json").read_bytes()
        _require(digest(raw_catalog) == CATALOG_SHA256)
        catalog = json.loads(raw_catalog)
        _require(catalog["result_id"] == RESULT_ID and catalog["evidence_commit"] == EVIDENCE_COMMIT)
        captured, index = {}, {}
        for entry in catalog["entries"]:
            name, eid = entry["file"], entry["evidence_id"]
            _require("/" not in name and "\\" not in name and ".." not in name and eid not in captured)
            checking_id = eid
            raw = root.joinpath(name).read_bytes()
            _require(len(raw) == entry["size"] and digest(raw) == entry["sha256"])
            captured[eid], index[eid] = raw, entry
        return catalog, captured, index
    except OSError:
        raise DomainError("BR1_RESULT_EVIDENCE_UNAVAILABLE", "Required sealed result evidence is unavailable", 503, details={"evidence_id": checking_id}) from None
    except (ValueError, KeyError, TypeError):
        raise DomainError("BR1_RESULT_EVIDENCE_CONFLICT", "Sealed result evidence failed identity or consistency checks", 409, details={"evidence_id": checking_id}) from None


def _money(value):
    # All operands here are already-projected decimal strings or declared cash.
    result = format(Decimal(str(value)), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result if result != "-0" else "0"


def _clock(value):
    at = datetime.fromisoformat(value)
    return (at.replace(tzinfo=SHANGHAI) if at.tzinfo is None else at.astimezone(SHANGHAI)).isoformat()


def _account(value):
    return dict(cash=value["cash"], equity=value["equity"], quantity=value["quantity"],
                sellable=value["closable"], average_price=value["average"],
                dividend_receivable=value["receivable"], native_daily_transaction_cost=value["cost"])


def _project(catalog, raw, index):
    def obj(eid):
        return json.loads(raw[eid])

    base, manifest, freeze, release = (obj(x) for x in ("baseline.receipt", "baseline.manifest", "freeze", "release"))
    author, checked, profile_doc = (obj(x) for x in ("author.validation", "checkpoint.validation", "profile"))
    review = obj("independent.summary")
    _require(review["candidate"] == dict(code_commit=freeze["code_commit"], code_tree=freeze["code_tree"],
             evidence_commit=catalog["evidence_commit"], evidence_tree=catalog["evidence_tree"], wheel_sha256=freeze["wheel_sha256"]))
    _require(review["release_sha256"] == freeze["release_sha256"] and review["review_outcome"] == "PASS_WITH_FIXED_SCOPE")
    _require(review["scope"] == "fixed conditional offline BR1 600000.XSHG 2020-01-03/2020-01-06; unchanged assumptions and parameters")
    _require(review["native_baseline"] == "PASS" and review["fault_recoveries"] == 4 and review["completed_reentries"] == 2)
    _require(review["native_orders"] == review["native_trades"] == 2 and review["final_cash_equity"] == base["state"]["equity"])
    _require(review["ad08_actual"] == "NOT_EXERCISED" and review["new_blocking_bugs"] == [])
    _require(all(review[k] == "BLOCKED" for k in ("strict_d1", "paper", "general_real_data_execution")))
    _require(review["f3_partial"] == "PRESERVED_UNSUPPORTED_EXTENSION")
    review_checksums = dict(line.split("  ", 1)[::-1] for line in raw["independent.checksums"].decode().splitlines())
    _require(review_checksums["summary.json"] == index["independent.summary"]["sha256"])
    _require(review_checksums["independent-review.md"] == index["independent.report"]["sha256"])
    _require(freeze["code_commit"] == CODE_COMMIT and author["candidate"] == freeze)
    _require(release["wheel_sha256"] == freeze["wheel_sha256"])
    _require(digest(raw["release"]) == freeze["release_sha256"] == manifest["release_sha256"])
    _require(manifest["release"] == release and manifest["identity"] == base["identity"])
    _require(manifest["status"] == "COMPLETED" and author["status"] == "PASS_FIXED_SCOPE" and checked["status"] == "PASS")
    _require(all(author[k] == "BLOCKED" for k in ("strict", "paper", "general_api")))
    _require(base["general_execution_permission"] is False and base["actual_ad08_calls"] == [])
    binding, p = base["binding"], profile_doc["profile"]
    # The profile digest uses its frozen compact JSON encoding (ASCII-only fields).
    _require(digest(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()) == profile_doc["profile_sha256"])
    _require(profile_doc["profile_sha256"] == binding["backend_profile_sha256"])
    _require(binding["historical_pit"] is False and binding["execution_permission"] is False)
    _require(base["listing_projection"]["source_absolute_date_status"] == "unknown")
    base_config = manifest["requested_native_config"]["base"]
    _require((base_config["start_date"], base_config["end_date"], base_config["frequency"], base_config["accounts"]) ==
             ("2020-01-03", "2020-01-06", "1d", {"stock": 10000}))
    initial = _money(base_config["accounts"]["stock"])
    checkpoints = {x["path"]: x for x in checked["checkpoint_artifacts"]}

    def checkpoint(eid, frozen_path):
        info = checkpoints[frozen_path]
        _require(info["sha256"] == index[eid]["sha256"] and info["payload_sha256_verified"] is True)
        header, payload = raw[eid].split(b"\n", 1)
        _require(json.loads(header) == dict(schema=1, identity=info["identity"], sha256=digest(payload)))
        # No pickle decode in the serving process. Semantic checks below rely on
        # the separately hashed frozen checkpoint-validation and committed receipt.
        return dict(evidence_id=eid, committed_cursor=info["cursor"], phase=info["phase"],
                    generation=info["generation"], complete=info["complete"],
                    committed_order_count=info["committed_orders"], committed_fill_count=info["committed_trades"])

    def completed(prefix, receipt):
        process, completion, teardown = (obj(prefix + "." + x) for x in ("process", "completion", "teardown"))
        _require(process["returncode"] == 0 and process["fresh_exec"] is True)
        _require(completion["identity"] == receipt["identity"] and completion["status"] == "COMPLETED")
        _require(teardown["host_closed"] is True and teardown["native_singleton_cleared"] is True)
        _require(process["pid"] == teardown["pid"])
        return process, completion

    base_process, base_completion = completed("baseline", base)
    base_cp = checkpoint("baseline.checkpoint", "runtime/baseline/native.chk")
    _require(base_cp["complete"] and base_cp["committed_cursor"] == 6 and base_cp["phase"] == "SETTLEMENT")
    _require(len(base["orders"]) == len(base["trades"]) == base_cp["committed_order_count"] == base_cp["committed_fill_count"] == 2)
    _require(len({o["order_id"] for o in base["orders"]}) == len({t["trade_id"] for t in base["trades"]}) == 2)
    _require({t["order_id"] for t in base["trades"]} == {o["order_id"] for o in base["orders"]})
    _require(base["states"][-1]["event"] == "POST_SETTLEMENT")
    _require(_account(base["states"][-1]) == _account(base["state"]))
    checks = {(c["case"], c["invocation"]): c for c in author["checks"]}
    _require(all(c["status"] == "PASS" and c["actual_ad08_calls"] == [] for c in author["checks"]))
    recoveries = []
    for case, fault, cursor in (("post-bar", "post_bar:2", 1), ("before-replace", "before_replace:2", 1),
                                ("after-replace", "after_replace:2", 2), ("cross-day", "after_event:4", 3)):
        receipt, killed = obj(case + ".receipt"), obj(case + ".killed")
        process, completion = completed(case, receipt)
        report = checks[(case, "resume-1")]
        _require(report["pid"] == process["pid"] != killed["pid"] and killed["returncode"] == -9)
        _require(report["runtime_epoch"] == completion["runtime_epoch"] == receipt["runtime_epoch"])
        _require(report["identity"] == receipt["identity"] and receipt["restore_generation"] == 1)
        _require(all(receipt[k] == base[k] for k in ("state", "orders", "trades", "states", "selection")))
        before = checkpoint(case + ".before", "runtime/" + case + "-before-resume/native.chk")
        after = checkpoint(case + ".checkpoint", "runtime/" + case + "-final/native.chk")
        _require(before["committed_cursor"] == cursor and not before["complete"])
        _require(after["complete"] and after["committed_cursor"] == 6 and after["generation"] == 1)
        _require(after["committed_order_count"] == after["committed_fill_count"] == 2)
        recoveries.append(dict(case_id=case, fault=fault, before=before, after=after,
                               final_equity=receipt["state"]["equity"], evidence_ids=("author.validation", "checkpoint.validation",
                               case + ".before", case + ".checkpoint", case + ".receipt", case + ".killed", case + ".process", case + ".completion", case + ".teardown")))
    _require(raw["baseline.checkpoint"] == raw["reentered.checkpoint"])
    _require(raw["baseline.receipt"] == raw["reentered.receipt"])
    _require(raw["baseline.trace"] == raw["reentered.trace"])  # Bytes only; never parse/account from traces.
    checkpoint("reentered.checkpoint", "runtime/baseline-after-reentry/native.chk")
    reentries, seen_pids, seen_epochs = [], {base_process["pid"]}, {base_completion["runtime_epoch"]}
    for attempt in ("reenter-1", "reenter-2"):
        process, completion = completed(attempt, base)
        report = checks[("baseline", attempt)]
        _require(report["checkpoint_receipt_trace_bytes_unchanged"] is True and completion["receipt_reentry"] is True)
        _require(report["pid"] == process["pid"] not in seen_pids and report["runtime_epoch"] == completion["runtime_epoch"] not in seen_epochs)
        seen_pids.add(process["pid"])
        seen_epochs.add(completion["runtime_epoch"])
        reentries.append(dict(case_id=attempt, evidence_ids=("author.validation", "baseline.checkpoint", "reentered.checkpoint",
                              "baseline.receipt", "reentered.receipt", "baseline.trace", "reentered.trace",
                              attempt + ".process", attempt + ".completion", attempt + ".teardown")))
    fills = [dict(fill_id=t["trade_id"], order_id=t["order_id"], side=t["side"], quantity=t["quantity"],
                  price=t["price"], commission=t["commission"], tax=t["tax"],
                  fee=_money(Decimal(t["commission"]) + Decimal(t["tax"])), filled_at=_clock(t["model_clock"])) for t in base["trades"]]
    commission = sum((Decimal(t["commission"]) for t in base["trades"]), Decimal(0))
    tax = sum((Decimal(t["tax"]) for t in base["trades"]), Decimal(0))
    return FrozenResult(
        result_id=RESULT_ID, evidence_version=catalog["evidence_version"], evidence_manifest_sha256=CATALOG_SHA256,
        evidence_commit=catalog["evidence_commit"], evidence_tree=catalog["evidence_tree"],
        scope=dict(initial_cash=initial, trading_dates=(base_config["start_date"], base_config["end_date"])),
        author_conclusion={}, independent_review=dict(execution_code_commit=CODE_COMMIT, evidence_commit=EVIDENCE_COMMIT,
             reviewed_at=review["reviewed_at_utc"], scope=review["scope"], summary_sha256=index["independent.summary"]["sha256"],
             report_sha256=index["independent.report"]["sha256"], negative_and_consistency_checks=review["negative_and_consistency_checks"],
             wheel_tests=review["wheel_tests"]),
        execution_identity=dict(code_commit=freeze["code_commit"], code_tree=freeze["code_tree"], wheel_sha256=freeze["wheel_sha256"],
                                release_sha256=freeze["release_sha256"], native_run_identity=base["identity"],
                                engine_commit="0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f",
                                engine_wheel_sha256=release["vendor_wheels"]["rqalpha-6.4.1-py3-none-any.whl"]),
        data_identity=dict(version=binding["installed_data_version"], commit=binding["installed_data_commit"], tree=binding["installed_data_tree"],
                           wheel_sha256=binding["installed_data_wheel_sha256"], dataset_id=binding["dataset_id"], manifest_sha256=binding["manifest_sha256"],
                           price_dataset_id=binding["strict_binding"]["price_dataset_id"], facts_component_id=binding["strict_binding"]["facts_component_id"],
                           owner_profile_sha256=binding["owner_profile_sha256"], listing_contract_sha256=binding["listing_contract_sha256"],
                           source_facts_owner_version=binding["original_receipt"]["owner_version"],
                           source_facts_dataset_id=binding["original_receipt"]["dataset_id"],
                           source_facts_manifest_sha256=binding["original_receipt"]["manifest_sha256"],
                           data_review_receipt_sha256=binding["data_review_receipt_sha256"],
                           listing_review_receipt_sha256=binding["listing_review_receipt_sha256"],
                           source_facts=[{k: f[k] for k in ("key", "conclusion", "evidence_status", "limitations")} for f in binding["original_receipt"]["facts"]]),
        profile=dict(sha256=profile_doc["profile_sha256"], strict_plan_sha256=p["strict_plan_sha256"], assumptions=p["assumptions"], limitations=(
            "Only this two-day fixed conditional research result is sealed; it does not admit general execution.",
            "A-EQ event absence is an assumption; A-AD08 depends on A-EQ and was not triggered by this run.",
            "Strict PIT, real minute coverage and amount capability are not established by this result.",
            "Absolute listing/delisting dates remain unknown; the internal bounded marker is not a source fact.",
            "F3 partial monitoring reconfiguration is an unsupported same-process extension; its original failure remains preserved.",
            "Independent PASS_WITH_FIXED_SCOPE covers only the specified dev7 execution/evidence; this read-only service revision is not included in that review.")),
        summary=dict(initial_cash=initial, final_cash=base["state"]["cash"], final_equity=base["state"]["equity"],
                     net_change=_money(Decimal(base["state"]["equity"]) - Decimal(initial)), commission_total=_money(commission),
                     tax_total=_money(tax), fee_total=_money(commission + tax), committed_order_count=len(base["orders"]), committed_fill_count=len(fills)),
        orders=base["orders"], fills=fills, account=_account(base["state"]),
        account_stages=[dict(sequence=i, phase=s["event"], logical_at=_clock(s["model_clock"]),
                             trading_date=datetime.fromisoformat(s["model_clock"]).date(), account=_account(s)) for i, s in enumerate(base["states"], 1)],
        recoveries=recoveries, reentries=reentries,
        evidence=[dict(evidence_id=e["evidence_id"], type=e["type"], sha256=e["sha256"], size_bytes=e["size"]) for e in catalog["entries"]])


def _frozen_result():
    catalog, raw, index = _verified_bundle()
    try:
        value = _project(catalog, raw, index)
        _require(digest(canonical(value.model_dump(mode="json"))) == SNAPSHOT_SHA256)
        return value
    except (ValueError, KeyError, TypeError, IndexError, ValidationError, ArithmeticError):
        raise DomainError("BR1_RESULT_EVIDENCE_CONFLICT", "Sealed result evidence failed identity or consistency checks", 409) from None


def result_detail(result_id):
    if result_id != RESULT_ID:
        raise DomainError("BR1_RESULT_NOT_FOUND", "Unknown sealed BR1 result", 404)
    serving_before = SERVING_SOURCE_SHA256
    if source_sha256() != serving_before:
        raise DomainError("BR1_RESULT_SERVICE_CHANGED", "Serving code changed; restart with the fixed service package", 409)
    value = _frozen_result()
    if source_sha256() != serving_before:
        raise DomainError("BR1_RESULT_SERVICE_CHANGED", "Serving code changed during the result read", 409)
    public = value.model_dump(mode="json")
    return BR1ResultDetail(**public, service_identity=ServingIdentity(backend_version=__version__, source_sha256=serving_before),
                           result_snapshot_sha256=digest(canonical(public)))


def list_results():
    detail = result_detail(RESULT_ID)
    item = ResultIndex(**{name: getattr(detail, name) for name in ResultIndex.model_fields})
    return BR1ResultList(service_identity=detail.service_identity, items=(item,))
