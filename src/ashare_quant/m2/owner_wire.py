"""Fixed public owner wire codec. Historical verification performs no Store reads."""

import hashlib
import json
import re
from datetime import datetime, time
from importlib import metadata
from importlib.resources import files

from ashare_data import DataError, M2ConsumerBinding, M2Read, M2ReadContext
from ashare_data.m2_payloads import economic_payload, query_payload
from ashare_data.m2_types import M2Document, canonical_hash

from ashare_quant.contracts import SHANGHAI, DomainError

from .common import digest, require
from .contracts import AD08Tuple, OwnerReadReceipt
from .owner_contracts import OwnerDocuments

RELEASE_SHA = "7ae85fbd491bee15028b11bc6b037a246643f8d6046d57c62419c9571eba44e0"


REVIEW_SHA = "8c27d758c59052d652a5ee9150819ecadca5654b56bfdfacaa5e409aa36dfd6b"


def review():
    raw = files("ashare_quant.m2").joinpath("owner-review.json").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == REVIEW_SHA, "M2_OWNER_REVIEW_CHANGED", "Fixed independent review receipt differs")
    value = json.loads(raw)
    require(value["release_receipt_sha256"] == RELEASE_SHA and value["status"] == "PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC",
            "M2_OWNER_REVIEW_CHANGED", "Independent review scope differs")
    return value


def release():
    try:
        raw = files("ashare_quant.m2").joinpath("owner-release.json").read_bytes()
    except OSError as exc:
        raise DomainError("M2_OWNER_RELEASE_UNAVAILABLE",
                          "The source preview does not distribute the fixed owner release receipt; see docs/research-prerequisites.md",
                          503) from exc
    require(hashlib.sha256(raw).hexdigest() == RELEASE_SHA, "M2_OWNER_RELEASE_CHANGED", "Fixed external release receipt differs")
    return json.loads(raw)


def verify_runtime():
    pin = release()
    require(metadata.version("ashare-data") == pin["version"], "M2_OWNER_RUNTIME_CHANGED", "Use the fixed non-editable owner wheel")
    provenance = json.loads(metadata.distribution("ashare-data").read_text("direct_url.json") or "{}")
    require("archive_info" in provenance and not provenance.get("dir_info", {}).get("editable", False),
            "M2_OWNER_RUNTIME_CHANGED", "Owner must be installed from a wheel, not an editable checkout")
    root = files("ashare_data")
    inventory = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob("*")) if p.suffix in (".py", ".json")}
    require(canonical_hash(inventory) == pin["runtime_code_sha256"], "M2_OWNER_RUNTIME_CHANGED", "Installed owner bytes differ from the fixed wheel")
    return pin


def owner_call(operation):
    try:
        return operation()
    except DataError as exc:
        raise DomainError(exc.code, exc.message, 422, details=exc.details, origin="ashare-data") from exc


def document(text):
    return M2Document(text.encode("utf-8"))


def pack(read):
    return OwnerDocuments(**{name: getattr(read, name).data.decode("utf-8") for name in OwnerDocuments.model_fields})


def unpack(packet):
    return M2Read(**{name: document(getattr(packet, name)) for name in OwnerDocuments.model_fields})


def owner_binding(entry, request, run_id):
    bound = entry.owner_binding
    require(bound is not None and request.data_binding_id == entry.binding.binding_id
            and request.window_id == entry.windows[0].window_id,
            "M2_OWNER_BINDING_MISMATCH", "One selected owner window must match this frozen request")
    plan = document(bound.window_plan).to_dict()
    return M2ConsumerBinding(owner_pin_sha256=document(bound.descriptor).sha256,
        window_id=bound.owner_window_id, window_plan_sha256=plan["window_plan_sha256"],
        run_binding_sha256=entry.binding.binding_id, run_id=run_id,
        consumer_request_sha256=digest(request), strategy_version_id=request.strategy_version_id,
        assumption_ack_bundle_sha256=request.assumptions.profile_sha256)


def epoch_number(epoch):
    require(re.fullmatch(r"owner-(0|[1-9][0-9]*)", epoch) is not None,
            "M2_OWNER_EPOCH_MAPPING", "Observed sessions use exact reversible owner-<integer> epochs")
    return int(epoch[6:])


def decision_context(entry, context, request):
    return M2ReadContext(**owner_binding(entry, request, context.run_id).to_dict(),
        current_date=context.trading_date, logical_at=context.logical_at, phase="DECIDE",
        query_end=datetime.combine(context.previous_open, time(23, 59, 59, 999999), SHANGHAI),
        visibility="assumed", consumer="strategy", event_cursor=context.event_cursor,
        restore_generation=context.generation, epoch=epoch_number(context.epoch))


def _self_hash(value, field):
    return value[field] == canonical_hash({k: v for k, v in value.items() if k != field})


def validate_binding(entry):
    """Fixed release/document association only, never inspect owner raw source files."""
    pin = release()
    accepted = review()
    require(accepted["windows"] == pin["windows"] and accepted["owner_pins"] == pin["owner_pins"],
            "M2_OWNER_REVIEW_CHANGED", "Independent review does not bind this release")
    bound = entry.owner_binding
    require(bound is not None and bound.release_sha256 == RELEASE_SHA,
            "M2_OWNER_BINDING_MISMATCH", "External release binding is missing")
    desc, plan, envelope = (document(getattr(bound, name)).to_dict() for name in ("descriptor", "window_plan", "envelope"))
    window = next(w for w in pin["windows"] if w["window_id"] == bound.owner_window_id)
    require(desc in pin["owner_pins"] and canonical_hash(desc) == window["owner_pin_sha256"]
            and _self_hash(plan, "window_plan_sha256") and plan["window_plan_sha256"] == window["window_plan_sha256"]
            and plan["owner_pin_sha256"] == window["owner_pin_sha256"]
            and plan["window_spec_sha256"] == window["window_spec_sha256"]
            and _self_hash(envelope, "envelope_sha256") and envelope["envelope_sha256"] == window["envelope_sha256"]
            and envelope["owner"] == desc and envelope["window_plan"] == plan
            and envelope["backend_execution_authorized"] is False and envelope["external_product_review"] == "PENDING"
            and [document(a).to_dict() for a in bound.assumption_refs] == window["assumption_refs"] == envelope["assumption_refs"],
            "M2_OWNER_BINDING_MISMATCH", "Owner descriptor/plan/envelope/ordered assumptions differ from release")
    b = entry.binding
    require((b.owner_version, b.owner_commit, b.owner_wheel_sha256, b.dataset_id, b.manifest_sha256,
             b.use_profile_sha256, b.query_contract_sha256, b.independent_review_sha256) == (
        pin["version"], pin["commit"], pin["wheel"]["sha256"], desc["dataset_id"], desc["manifest_sha256"],
        desc["profile_sha256"], desc["query_contract_sha256"], REVIEW_SHA),
        "M2_OWNER_BINDING_MISMATCH", "Backend binding must preserve owner identity and conditional independent review")
    require(b.backend_policy_sha256 == digest({"policy": "m2.owner_codec_reviewed.v1", "release": RELEASE_SHA, "review": REVIEW_SHA,
            "owner_pin": canonical_hash(desc), "window_plan": plan["window_plan_sha256"], "envelope": window["envelope_sha256"]}),
            "M2_OWNER_BINDING_MISMATCH", "Backend preparation policy differs")
    links = plan["geometry"]["calendar_links"]
    require(len(entry.windows) == 1 and len(links) == len(entry.windows[0].days)
            and entry.windows[0].owner_calendar_sha256 == plan["window_spec_sha256"]
            and entry.windows[0].read_domain_sha256 == canonical_hash(plan["read_roles"])
            and all(d.owner_evidence_sha256 == canonical_hash({"plan": plan["window_plan_sha256"], "link": link})
                    and (d.trading_date.isoformat(), d.previous_open.isoformat(), d.next_open.isoformat()) ==
                    (link["current"], link["previous_open"], link["next_open"])
                    for d, link in zip(entry.windows[0].days, links, strict=True)),
            "M2_OWNER_BINDING_MISMATCH", "Backend window must preserve every owner T/P/next")
    profile = entry.profile(entry.windows[0].window_id)
    require(profile.visibility_policy_sha256 == canonical_hash(envelope["assumption_refs"])
            and profile.matching_policy_sha256 == digest("m2.A-CLOSE.current_close.conditional.v1")
            and profile.fee_policy_sha256 == digest("m2.A-COST.rq641.fixed.v1")
            and profile.pool_policy_sha256 == digest("m2.A-POOL.600000.fixed.v1"),
            "M2_OWNER_BINDING_MISMATCH", "Owner ack and backend model policies differ")
    return desc, plan, envelope


def receipt_projection(read, entry, context):
    q = read.query.to_dict()
    call = q["call"]
    return OwnerReadReceipt(binding_id=entry.binding.binding_id, dataset_id=entry.binding.dataset_id,
        use_profile_sha256=entry.binding.use_profile_sha256, query_contract_sha256=entry.binding.query_contract_sha256,
        window_id=context.window_id, role="decision_previous_close",
        call=AD08Tuple(security=call["security"], trading_date=call["trade_date"], history_date=call["history_dt"],
            adjust_origin=call["adjust_orig"], frequency=call["frequency"], field=call["field"], count=call["bar_count"],
            include_now=call["include_now"], skip_suspended=call["skip_suspended"], adjustment_requested=call["adjustment_requested"]),
        authorization_sha256=context.authorization_sha256, generation=context.generation,
        value=str(read.value), source_row_sha256=read.sources.sha256,  # legacy projection: full Sources digest
        evidence_kind="observed", assumption_ids=tuple(a["assumption_id"] for a in read.evidence.to_dict()["assumption_refs"]))


def validate_historical_read(packet, receipt, entry, context, request):
    """Integrity/link check of committed public documents, not a new query/authorization."""
    def validate():
        desc, plan, envelope = validate_binding(entry)
        read = unpack(packet)
        q, r = query_payload(read.query.to_dict()), economic_payload(read.result.to_dict())
        expected_context = decision_context(entry, context, request)
        e = read.evidence.to_dict()
        a = read.authorization.to_dict()
        require(read.query == M2Document.of(q) and read.result == M2Document.of(r)
                and q["operation"] == r["operation"] == "decision_prev_close"
                and q["current_date"] == r["current_date"] == context.trading_date.isoformat()
                and q["target_date"] == r["target_date"] == context.previous_open.isoformat()
                and q["role"] == r["role"] == "previous"
                and r["value_basis"] == "previous_raw_close_times_scoped_ratio" and r["scoped_ratio"] == "1",
                "M2_OWNER_PACKET_MISMATCH", "Historical query/result must be this selected P-only decision")
        expected = dict(schema="m2.read-evidence.v1", owner=desc, owner_pin_sha256=canonical_hash(desc),
            window_id=entry.owner_binding.owner_window_id, window_plan_sha256=plan["window_plan_sha256"],
            query_sha256=read.query.sha256, result_sha256=read.result.sha256,
            source_references_sha256=read.sources.sha256, role=q["role"], current_date=q["current_date"],
            target_date=q["target_date"], quality=envelope["original_quality"], assumption_refs=envelope["assumption_refs"],
            source_evidence_kind="source_claim_unverified")
        require(e == {**expected, "evidence_sha256": canonical_hash(expected)},
                "M2_OWNER_PACKET_MISMATCH", "Historical stable owner evidence differs")
        require(a["authorization_kind"] in ("fresh_read", "cache_revalidation"),
                "M2_OWNER_PACKET_MISMATCH", "Unknown authorization kind")
        expected_auth = dict(schema="m2.read-authorization.v1", evidence_sha256=e["evidence_sha256"],
            context=expected_context.to_dict(), authorization_kind=a["authorization_kind"],
            data_read_permission="conditional_research", execution_permission=False)
        require(a == {**expected_auth, "authorization_sha256": canonical_hash(expected_auth)}
                and M2ReadContext.from_dict(a["context"]) == expected_context
                and receipt_projection(read, entry, context) == receipt,
                "M2_OWNER_PACKET_MISMATCH", "Historical authorization/context/local projection differs")
    try:
        owner_call(validate)
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("M2_OWNER_PACKET_MISMATCH", "Invalid historical owner packet", 409) from exc


def validate_codec_observation(text, native_value):
    from decimal import Decimal

    import numpy as np
    require(text is not None, "M2_OWNER_DOCUMENTS_REQUIRED", "Preserve the public native conversion observation")
    value = owner_call(lambda: document(text).to_dict())
    require(set(value) == {"codec_version", "native_type", "native_shape", "native_dtype", "native_repr",
                           "normalized_decimal", "native_call_observed"}
            and value["codec_version"] == "m2.rq641.codec.v1" and value["native_call_observed"] is False
            and value["native_type"] in ("float", "float64") and value["native_shape"] == []
            and value["native_dtype"] in ("float64", "python.float64")
            and value["native_repr"] == repr(float(native_value) if value["native_type"] == "float" else np.float64(native_value))
            and Decimal(value["normalized_decimal"]) == Decimal(native_value),
            "M2_OWNER_PACKET_MISMATCH", "Prepared codec metadata must preserve actual scalar conversion without claiming native AD08")
