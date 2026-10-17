"""Recorded trusted-Python decisions. Reading these records never executes code."""

from typing import Any, Literal

from pydantic import Field

from ashare_quant.backtests.daily.contracts import Sha, Signal
from ashare_quant.contracts import Contract
from ashare_quant.storage.canonical import digest, require


class PythonDecisionReceipt(Contract):
    verification_kind: Literal["trusted_python_record.v1"] = "trusted_python_record.v1"
    strategy_version_id: Sha
    source_sha256: Sha
    sdk_sha256: Sha
    parameters_sha256: Sha
    input: dict[str, Any]
    input_sha256: Sha
    signal: Signal
    strategy_reason: str = Field(max_length=256)
    output_sha256: Sha


def inputs(session, context, account, read):
    days = session.days
    require(
        session.data_binding_sha256 is not None,
        "PYTHON_INPUT_INVALID",
        "Frozen local data binding required",
    )
    return dict(
        logical_at=context.logical_at.isoformat(),
        trading_date=context.trading_date.isoformat(),
        previous_open=context.previous_open.isoformat(),
        day_index=next(i for i, day in enumerate(days) if day.trading_date == context.trading_date),
        security="600000.XSHG",
        prior_account=dict(
            cash=account.cash,
            quantity=account.quantity,
            snapshot_as_of=account.account_as_of.isoformat(),
            snapshot_sha256=account.snapshot_sha256,
        ),
        data=dict(
            previous_close=read.native_value,
            price_date=context.previous_open.isoformat(),
            data_binding_sha256=session.data_binding_sha256,
            read_evidence_sha256=digest(read),
        ),
    )


def verify(receipt, session, context, account, read):
    require(
        receipt is not None,
        "PYTHON_RECEIPT_REQUIRED",
        "Committed Python decision needs its typed execution record",
    )
    expected = inputs(session, context, account, read)
    require(
        receipt.strategy_version_id == session.strategy.version_id
        and receipt.source_sha256 == session.strategy.source_sha256
        and receipt.sdk_sha256 == session.strategy.definition.sdk_sha256
        and receipt.parameters_sha256 == digest(session.strategy_parameters)
        and receipt.input == expected
        and receipt.input_sha256 == digest(expected)
        and receipt.output_sha256
        == digest({"signal": receipt.signal, "reason": receipt.strategy_reason})
        and receipt.signal.reason in {"python_target", "python_no_change"},
        "PYTHON_RECEIPT_MISMATCH",
        "Recorded Python input, output or frozen identity differs",
    )
    return receipt.signal
