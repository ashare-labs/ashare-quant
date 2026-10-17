"""Fixed public owner wire codec. Historical verification performs no Store reads."""

import json
import re
from datetime import datetime, time

from ashare_data import DataError, PortableConsumerBinding, PortableRead, PortableReadContext
from ashare_data.research.payloads import economic_payload, query_payload
from ashare_data.research.portable_types import PortableDocument, canonical_hash

from ashare_quant.backtests.daily.contracts import AD08Tuple, OwnerReadReceipt
from ashare_quant.contracts import SHANGHAI, DomainError
from ashare_quant.providers.daily.owner_contracts import OwnerDocuments
from ashare_quant.storage.canonical import digest, require


def source_pin():
    from ashare_quant.providers.runtime import pin
    return pin()


def verify_runtime():
    from ashare_quant.providers.runtime import verify
    return verify()


def owner_call(operation):
    try:
        return operation()
    except DataError as exc:
        raise DomainError(
            exc.code, exc.message, 422, details=exc.details, origin="ashare-data"
        ) from exc


def document(text):
    return PortableDocument(text.encode("utf-8"))


def pack(read):
    return OwnerDocuments(
        **{name: getattr(read, name).data.decode("utf-8") for name in OwnerDocuments.model_fields}
    )


def unpack(packet):
    return PortableRead(**{name: document(getattr(packet, name)) for name in OwnerDocuments.model_fields})


def owner_binding(entry, request, run_id):
    bound = entry.owner_binding
    require(
        bound is not None
        and request.data_binding_id == entry.binding.binding_id
        and request.window_id == entry.windows[0].window_id,
        "M2_OWNER_BINDING_MISMATCH",
        "One selected owner window must match this frozen request",
    )
    plan = document(bound.window_plan).to_dict()
    return PortableConsumerBinding(
        owner_pin_sha256=document(bound.descriptor).sha256,
        window_id=bound.owner_window_id,
        window_plan_sha256=plan["window_plan_sha256"],
        run_binding_sha256=entry.binding.binding_id,
        run_id=run_id,
        consumer_request_sha256=digest(request),
        strategy_version_id=request.strategy_version_id,
        assumption_ack_bundle_sha256=request.assumptions.profile_sha256,
    )


def epoch_number(epoch):
    require(
        re.fullmatch(r"owner-(0|[1-9][0-9]*)", epoch) is not None,
        "M2_OWNER_EPOCH_MAPPING",
        "Observed sessions use exact reversible owner-<integer> epochs",
    )
    return int(epoch[6:])


def decision_context(entry, context, request):
    return PortableReadContext(
        **owner_binding(entry, request, context.run_id).to_dict(),
        current_date=context.trading_date,
        logical_at=context.logical_at,
        phase="DECIDE",
        query_end=datetime.combine(context.previous_open, time(23, 59, 59, 999999), SHANGHAI),
        visibility="assumed",
        consumer="strategy",
        event_cursor=context.event_cursor,
        restore_generation=context.generation,
        epoch=epoch_number(context.epoch),
    )


def _self_hash(value, field):
    return value[field] == canonical_hash({k: v for k, v in value.items() if k != field})


def validate_binding(entry):
    """Check immutable local metadata; never query data or grant a new read."""
    from ashare_quant.providers.daily.portable_policy import POLICY_SHA, validate_plan

    pin = source_pin()
    bound = entry.owner_binding
    require(
        bound is not None
        and bound.policy_sha256 == POLICY_SHA
        and bound.owner_runtime_sha256 == pin["runtime_code_sha256"],
        "M2_OWNER_BINDING_MISMATCH",
        "Local source/scope binding is missing",
    )
    desc, plan, envelope = (
        document(getattr(bound, name)).to_dict()
        for name in ("descriptor", "window_plan", "envelope")
    )
    require(
        validate_plan(plan) == bound.owner_window_id
        and desc["version"] == pin["portable_owner_version"]
        and desc["runtime_code_sha256"] == pin["runtime_code_sha256"]
        and desc["query_contract_sha256"] == pin["query_contract_sha256"]
        and plan["query_contract_sha256"] == desc["query_contract_sha256"]
        and _self_hash(plan, "window_plan_sha256")
        and plan["owner_pin_sha256"] == canonical_hash(desc)
        and _self_hash(envelope, "envelope_sha256")
        and envelope["owner"] == desc
        and envelope["window_plan"] == plan
        and envelope["backend_execution_authorized"] is False
        and envelope["external_product_review"] == "PENDING"
        and [document(a).to_dict() for a in bound.assumption_refs] == envelope["assumption_refs"]
        and [document(a).to_dict()["assumption_id"] for a in bound.assumption_refs]
        == ["A-AD08", "A-EQ", "A-NORMAL", "A-VIS"]
        and json.loads(entry.original_unknowns[0]) == envelope["original_quality"],
        "M2_OWNER_BINDING_MISMATCH",
        "Owner documents, scope, quality or assumptions differ",
    )
    b = entry.binding
    require(
        (
            b.owner_version,
            b.owner_commit,
            b.owner_runtime_sha256,
            b.dataset_id,
            b.manifest_sha256,
            b.use_profile_sha256,
            b.query_contract_sha256,
        )
        == (
            pin["version"],
            pin["commit"],
            pin["runtime_code_sha256"],
            desc["dataset_id"],
            desc["manifest_sha256"],
            desc["profile_sha256"],
            desc["query_contract_sha256"],
        )
        and b.owner_wheel_sha256 is None
        and b.independent_review_sha256 == pin["contract_review"]["receipt_sha256"]
        and b.backend_policy_sha256
        == digest({"policy": POLICY_SHA, "owner_binding": bound.identity}),
        "M2_OWNER_BINDING_MISMATCH",
        "Preserve exact candidate source and its actual review status",
    )
    links = plan["geometry"]["calendar_links"]
    require(
        len(entry.windows) == 1
        and len(links) == len(entry.windows[0].days)
        and entry.windows[0].owner_calendar_sha256 == plan["window_spec_sha256"]
        and entry.windows[0].read_domain_sha256 == canonical_hash(plan["read_roles"])
        and all(
            d.owner_evidence_sha256
            == canonical_hash({"plan": plan["window_plan_sha256"], "link": link})
            and (d.trading_date.isoformat(), d.previous_open.isoformat(), d.next_open.isoformat())
            == (link["current"], link["previous_open"], link["next_open"])
            for d, link in zip(entry.windows[0].days, links, strict=True)
        ),
        "M2_OWNER_BINDING_MISMATCH",
        "Backend window must preserve every owner T/P/next",
    )
    profile = entry.profile(entry.windows[0].window_id)
    require(
        profile.visibility_policy_sha256 == canonical_hash(envelope["assumption_refs"])
        and profile.matching_policy_sha256 == digest("m2.A-CLOSE.current_close.conditional.v1")
        and profile.fee_policy_sha256 == digest("m2.A-COST.rq641.fixed.v1")
        and profile.pool_policy_sha256 == digest("m2.A-POOL.600000.fixed.v1"),
        "M2_OWNER_BINDING_MISMATCH",
        "Owner ack and backend model policies differ",
    )
    return desc, plan, envelope


def receipt_projection(read, entry, context):
    q = read.query.to_dict()
    call = q["call"]
    return OwnerReadReceipt(
        binding_id=entry.binding.binding_id,
        dataset_id=entry.binding.dataset_id,
        use_profile_sha256=entry.binding.use_profile_sha256,
        query_contract_sha256=entry.binding.query_contract_sha256,
        window_id=context.window_id,
        role="decision_previous_close",
        call=AD08Tuple(
            security=call["security"],
            trading_date=call["trade_date"],
            history_date=call["history_dt"],
            adjust_origin=call["adjust_orig"],
            frequency=call["frequency"],
            field=call["field"],
            count=call["bar_count"],
            include_now=call["include_now"],
            skip_suspended=call["skip_suspended"],
            adjustment_requested=call["adjustment_requested"],
        ),
        authorization_sha256=context.authorization_sha256,
        generation=context.generation,
        value=str(read.value),
        source_row_sha256=read.sources.sha256,  # legacy projection: full Sources digest
        evidence_kind="observed",
        assumption_ids=tuple(
            a["assumption_id"] for a in read.evidence.to_dict()["assumption_refs"]
        ),
    )


def validate_historical_read(packet, receipt, entry, context, request):
    """Integrity/link check of committed public documents, not a new query/authorization."""

    def validate():
        desc, plan, envelope = validate_binding(entry)
        read = unpack(packet)
        q, r = query_payload(read.query.to_dict()), economic_payload(read.result.to_dict())
        expected_context = decision_context(entry, context, request)
        e = read.evidence.to_dict()
        a = read.authorization.to_dict()
        require(
            read.query == PortableDocument.of(q)
            and read.result == PortableDocument.of(r)
            and q["operation"] == r["operation"] == "decision_prev_close"
            and q["current_date"] == r["current_date"] == context.trading_date.isoformat()
            and q["target_date"] == r["target_date"] == context.previous_open.isoformat()
            and q["role"] == r["role"] == "previous"
            and r["value_basis"] == "previous_raw_close_times_scoped_ratio"
            and r["scoped_ratio"] == "1",
            "M2_OWNER_PACKET_MISMATCH",
            "Historical query/result must be this selected P-only decision",
        )
        expected = dict(
            schema="portable.read-evidence.v1",
            owner=desc,
            owner_pin_sha256=canonical_hash(desc),
            window_id=entry.owner_binding.owner_window_id,
            window_plan_sha256=plan["window_plan_sha256"],
            query_sha256=read.query.sha256,
            result_sha256=read.result.sha256,
            source_references_sha256=read.sources.sha256,
            role=q["role"],
            current_date=q["current_date"],
            target_date=q["target_date"],
            quality=envelope["original_quality"],
            assumption_refs=envelope["assumption_refs"],
            source_evidence_kind="source_claim_unverified",
        )
        require(
            e == {**expected, "evidence_sha256": canonical_hash(expected)},
            "M2_OWNER_PACKET_MISMATCH",
            "Historical stable owner evidence differs",
        )
        require(
            a["authorization_kind"] in ("fresh_read", "cache_revalidation"),
            "M2_OWNER_PACKET_MISMATCH",
            "Unknown authorization kind",
        )
        expected_auth = dict(
            schema="portable.read-authorization.v1",
            evidence_sha256=e["evidence_sha256"],
            context=expected_context.to_dict(),
            authorization_kind=a["authorization_kind"],
            data_read_permission="conditional_research",
            execution_permission=False,
        )
        require(
            a == {**expected_auth, "authorization_sha256": canonical_hash(expected_auth)}
            and PortableReadContext.from_dict(a["context"]) == expected_context
            and receipt_projection(read, entry, context) == receipt,
            "M2_OWNER_PACKET_MISMATCH",
            "Historical authorization/context/local projection differs",
        )

    try:
        owner_call(validate)
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError(
            "M2_OWNER_PACKET_MISMATCH", "Invalid historical owner packet", 409
        ) from exc


def validate_codec_observation(text, native_value):
    from decimal import Decimal

    import numpy as np

    require(
        text is not None,
        "M2_OWNER_DOCUMENTS_REQUIRED",
        "Preserve the public native conversion observation",
    )
    value = owner_call(lambda: document(text).to_dict())
    require(
        set(value)
        == {
            "codec_version",
            "native_type",
            "native_shape",
            "native_dtype",
            "native_repr",
            "normalized_decimal",
            "native_call_observed",
        }
        and value["codec_version"] == "portable.rq641.codec.v1"
        and value["native_call_observed"] is False
        and value["native_type"] in ("float", "float64")
        and value["native_shape"] == []
        and value["native_dtype"] in ("float64", "python.float64")
        and value["native_repr"]
        == repr(
            float(native_value) if value["native_type"] == "float" else np.float64(native_value)
        )
        and Decimal(value["normalized_decimal"]) == Decimal(native_value),
        "M2_OWNER_PACKET_MISMATCH",
        "Prepared codec metadata must preserve actual scalar conversion without claiming native AD08",
    )
