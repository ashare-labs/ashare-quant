"""Invoke only from an uncommitted native DECIDE. Trusted owner code, no sandbox."""

import time
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from ashare_quant.backtests.daily.quantity import valid_target
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import atomic
from ashare_quant.storage.canonical import digest
from ashare_quant.strategies.api import (
    DecisionContext,
    DecisionData,
    NoChange,
    PriorAccount,
    TargetShares,
)
from tests.support.legacy_backtests.contracts import Signal
from tests.support.legacy_python.decision import PythonDecisionReceipt, inputs
from tests.support.legacy_python.spec import task_source


def invoke(session, context, account, read, root, invocation):
    raw = task_source(root, session.strategy)
    frozen = inputs(session, context, account, read)
    phase_path = root / (invocation + ".python-phase.json")
    phase = "MODULE_LOAD"

    def mark(name):
        atomic(
            phase_path,
            dict(
                phase=name,
                started_monotonic=time.monotonic(),
                strategy_version_id=session.strategy.version_id,
                logical_at=frozen["logical_at"],
                input_sha256=digest(frozen),
            ),
        )

    try:
        mark(phase)
        namespace = {"__name__": "ashare_user_strategy", "__file__": "strategy-source.py"}
        # Exact frozen bytes; no import of the user's original path or registry.
        exec(compile(raw.decode("utf-8"), "strategy-source.py", "exec"), namespace)
        phase = "CALLBACK"
        mark(phase)
        callback = namespace.get("on_decision")
        if not callable(callback):
            raise DomainError(
                "PYTHON_ENTRYPOINT_INVALID", "Frozen on_decision is not callable", 422
            )
        prior = frozen["prior_account"]
        sdk_context = DecisionContext(
            datetime.fromisoformat(frozen["logical_at"]),
            date.fromisoformat(frozen["trading_date"]),
            date.fromisoformat(frozen["previous_open"]),
            frozen["day_index"],
            frozen["security"],
            PriorAccount(
                Decimal(prior["cash"]),
                prior["quantity"],
                datetime.fromisoformat(prior["snapshot_as_of"]),
                prior["snapshot_sha256"],
            ),
        )
        sdk_data = DecisionData(
            Decimal(frozen["data"]["previous_close"]),
            date.fromisoformat(frozen["data"]["price_date"]),
            session.data_binding_sha256,
            frozen["data"]["read_evidence_sha256"],
        )
        result = callback(
            sdk_context, sdk_data, MappingProxyType(dict(session.strategy_parameters))
        )
        if (
            type(result) not in (TargetShares, NoChange)
            or type(result.reason) is not str
            or len(result.reason) > 256
        ):
            raise DomainError(
                "PYTHON_OUTPUT_INVALID",
                "Return TargetShares or NoChange with a reason up to 256 characters",
                422,
            )
        if type(result) is TargetShares:
            if not valid_target(result.quantity):
                raise DomainError(
                    "PYTHON_OUTPUT_INVALID",
                    "Target must be a nonnegative, exactly representable whole lot",
                    422,
                )
            signal = Signal(
                action="target_shares", target_shares=result.quantity, reason="python_target"
            )
        else:
            signal = Signal(action="no_change", reason="python_no_change")
        return PythonDecisionReceipt(
            strategy_version_id=session.strategy.version_id,
            source_sha256=session.strategy.source_sha256,
            sdk_sha256=session.strategy.definition.sdk_sha256,
            parameters_sha256=digest(session.strategy_parameters),
            input=frozen,
            input_sha256=digest(frozen),
            signal=signal,
            strategy_reason=result.reason,
            output_sha256=digest({"signal": signal, "reason": result.reason}),
        )
    except BaseException as exc:
        code = (
            exc.code
            if isinstance(exc, DomainError)
            else ("PYTHON_LOAD_ERROR" if phase == "MODULE_LOAD" else "PYTHON_CALLBACK_ERROR")
        )
        line = None
        tb = exc.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename == "strategy-source.py":
                line = tb.tb_lineno
            tb = tb.tb_next
        details = dict(
            phase=phase,
            strategy_version_id=session.strategy.version_id,
            logical_at=frozen["logical_at"],
            input_sha256=digest(frozen),
            exception_type=type(exc).__name__,
            source_line=line,
        )
        raise DomainError(code, str(exc)[:1000], details=details) from None
    finally:
        mark("IDLE")
