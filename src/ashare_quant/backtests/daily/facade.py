"""Synthetic-testable native-return contract. No RQ or data-owner implementation.

The future instrumented native port must capture the actual DataProxy return and
its owner receipt together. This module cannot start RQ or mark real AD08 passed.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from numbers import Real
from typing import Protocol

from ashare_quant.backtests.daily.catalogue import PreparationEntry, check_context
from ashare_quant.backtests.daily.contracts import (
    AD08Tuple,
    BacktestRequest,
    DecisionContext,
    NativeCallFact,
    OwnerReadReceipt,
    ReadEvidence,
)
from ashare_quant.contracts import DomainError
from ashare_quant.providers.daily.owner_contracts import OwnerDocuments
from ashare_quant.storage.canonical import decimal_text, digest, require


def normalize_native(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0 < value < 1e17:
        raise DomainError("M2_INVALID_NATIVE_VALUE", "Native previous close is missing, nonfinite or out of range", 422)
    normalized = decimal_text(format(Decimal(str(float(value))), "f"))
    require("." not in normalized or len(normalized.split(".")[1]) <= 8,
            "M2_NATIVE_PRECISION_UNSUPPORTED", "No rounding or epsilon may conceal a native/owner difference")
    return normalized


def expected_call(context):
    return AD08Tuple(trading_date=context.trading_date, history_date=context.previous_open,
                     adjust_origin=context.trading_date)


def check_owner(receipt, entry, context):
    binding = entry.binding
    require((receipt.binding_id, receipt.dataset_id, receipt.use_profile_sha256, receipt.query_contract_sha256,
             receipt.window_id, receipt.call, receipt.authorization_sha256, receipt.generation) == (
                 binding.binding_id, binding.dataset_id, binding.use_profile_sha256, binding.query_contract_sha256,
                 context.window_id, expected_call(context), context.authorization_sha256, context.generation),
            "M2_READ_BINDING_MISMATCH", "Owner read receipt does not belong to this context and WindowPlan")
    require(receipt.evidence_kind == entry.evidence_kind, "M2_READ_KIND_MISMATCH", "Synthetic evidence cannot upgrade source kind")


@dataclass(frozen=True)
class NativeObservation:
    value: object
    call: AD08Tuple
    native_call_id: str
    owner_receipt: OwnerReadReceipt
    # Native cache hits carry their actual first-call object, never a dangling attempt-log ID.
    origin_fact: NativeCallFact | None = None
    owner_documents: OwnerDocuments | None = None
    codec_observation: str | None = None


class NativePreviousClosePort(Protocol):
    def current_context(self) -> DecisionContext: ...
    def read(self, context: DecisionContext, call: AD08Tuple) -> NativeObservation: ...


def read_previous_close(port: NativePreviousClosePort, entry: PreparationEntry, context: DecisionContext,
                        *, run_id: str, request: BacktestRequest) -> ReadEvidence:
    check_context(entry, context, run_id=run_id, request=request)
    require(port.current_context() == context, "M2_READ_AUTHORITY_MISMATCH", "Historical evidence is not current read authority")
    observed = port.read(context, expected_call(context))
    require(port.current_context() == context, "M2_READ_AUTHORITY_MISMATCH", "Native context changed during the read")
    require(type(observed) is NativeObservation and observed.call == expected_call(context),
            "M2_NATIVE_CALL_MISMATCH", "Observed native call differs from the allowed AD08 tuple")
    value = normalize_native(observed.value)  # Sole source of the strategy's price.
    check_owner(observed.owner_receipt, entry, context)
    require(Decimal(value) == Decimal(observed.owner_receipt.value),
            "M2_NATIVE_OWNER_VALUE_MISMATCH", "Native return and owner value differ at the declared precision")
    origin = observed.origin_fact or NativeCallFact(origin_context=context, call=observed.call,
        owner_receipt=observed.owner_receipt, native_return_repr=repr(float(observed.value)),
        native_value=value, native_call_id=observed.native_call_id, owner_documents=observed.owner_documents,
        codec_observation=observed.codec_observation)
    result = ReadEvidence(call=observed.call, owner_receipt=observed.owner_receipt,
        native_return_repr=repr(float(observed.value)), native_value=value,
        native_call_id=observed.native_call_id, origin_authorization_sha256=origin.origin_context.authorization_sha256,
        consumption_authorization_sha256=context.authorization_sha256,
        origin_generation=origin.origin_context.generation, consumption_generation=context.generation,
        cache_hit=observed.origin_fact is not None,
        source_call_evidence_sha256=digest(origin) if observed.origin_fact else None, origin_fact=origin,
        owner_documents=observed.owner_documents,
        codec_observation=observed.codec_observation,
        verification_level="owner_codec_only" if observed.owner_documents else "synthetic_contract_only")
    validate_evidence(result, entry, context, run_id=run_id, request=request)
    return result


def validate_evidence(evidence, entry, context, *, run_id, request: BacktestRequest):
    """Historical verification is distinct from permission to make a new read."""
    check_context(entry, context, run_id=run_id, request=request)
    check_owner(evidence.owner_receipt, entry, context)
    origin = evidence.origin_fact
    check_context(entry, origin.origin_context, run_id=run_id, request=request)
    check_owner(origin.owner_receipt, entry, origin.origin_context)
    require(evidence.owner_receipt.stable_evidence == origin.owner_receipt.stable_evidence,
            "M2_STABLE_EVIDENCE_MISMATCH", "Reauthorization cannot change stable read evidence")
    require(evidence.call == origin.call == expected_call(context), "M2_READ_BINDING_MISMATCH", "Cached call scope differs")
    require(evidence.consumption_authorization_sha256 == context.authorization_sha256
            and evidence.consumption_generation == context.generation
            and evidence.origin_authorization_sha256 == origin.origin_context.authorization_sha256
            and evidence.origin_generation == origin.origin_context.generation
            and origin.origin_context.event_cursor <= context.event_cursor
            and origin.origin_context.generation <= context.generation
            and (origin.origin_context.generation < context.generation or origin.origin_context.epoch == context.epoch),
            "M2_READ_AUTHORITY_MISMATCH", "Read evidence refers to a different authority")
    try:
        values = (normalize_native(float(evidence.native_return_repr)), normalize_native(float(origin.native_return_repr)))
    except (ValueError, TypeError):
        raise DomainError("M2_INVALID_NATIVE_VALUE", "Invalid persisted native return representation", 409) from None
    require(values[0] == values[1] == evidence.native_value == origin.native_value
            and Decimal(evidence.owner_receipt.value) == Decimal(evidence.native_value)
            and Decimal(origin.owner_receipt.value) == Decimal(origin.native_value)
            and evidence.native_call_id == origin.native_call_id,
            "M2_NATIVE_OWNER_VALUE_MISMATCH", "Persisted native value/receipt mismatch")
    if evidence.cache_hit:
        require(evidence.source_call_evidence_sha256 == digest(origin), "M2_CACHE_EVIDENCE_MISSING", "Cache hit must embed the first native call fact")
    else:
        require(evidence.source_call_evidence_sha256 is None and origin.origin_context == context,
                "M2_READ_AUTHORITY_MISMATCH", "New call must belong to the current authority")
    if entry.evidence_kind == "observed":
        from ashare_quant.providers.daily.owner_wire import (
            validate_codec_observation,
            validate_historical_read,
        )
        require(evidence.verification_level == "owner_codec_only" and evidence.owner_documents is not None
                and origin.owner_documents is not None, "M2_OWNER_DOCUMENTS_REQUIRED", "Keep all five public owner documents")
        validate_historical_read(evidence.owner_documents, evidence.owner_receipt, entry, context, request)
        validate_historical_read(origin.owner_documents, origin.owner_receipt, entry, origin.origin_context, request)
        validate_codec_observation(evidence.codec_observation, evidence.native_value)
        validate_codec_observation(origin.codec_observation, origin.native_value)
        require(evidence.owner_documents.stable() == origin.owner_documents.stable(),
                "M2_STABLE_EVIDENCE_MISMATCH", "All four stable owner documents must match the first read")
    else:
        require(evidence.owner_documents is None and origin.owner_documents is None
                and evidence.codec_observation is None and origin.codec_observation is None
                and evidence.verification_level == "synthetic_contract_only",
                "M2_READ_KIND_MISMATCH", "Synthetic preparation cannot carry observed owner evidence")
