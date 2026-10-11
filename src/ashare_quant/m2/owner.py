"""Read-only fixed owner preparation using public APIs, never raw source files."""

from dataclasses import dataclass
from datetime import datetime, time

from ashare_quant.contracts import SHANGHAI, DomainError

from .catalogue import check_context
from .common import require
from .contracts import AssumptionAck, BacktestRequest
from .journal import Session
from .owner_wire import (
    decision_context,
    document,
    epoch_number,
    owner_binding,
    owner_call,
    validate_binding,
    verify_runtime,
)


def require_backend_execution():
    raise DomainError(
        "M2_NATIVE_INTEGRATION_PENDING",
        "Owner codec preparation does not authorize native execution",
        422,
    )


def prepare(store, strategy, *, run_id, initial_cash, key, entry, strategy_parameters=None, data_binding_sha256=None, end_date=None):
    verify_runtime()
    validate_binding(entry)
    request = BacktestRequest(
        end_date=end_date,
        strategy_version_id=strategy.version_id,
        data_binding_id=entry.binding.binding_id,
        window_id=entry.windows[0].window_id,
        initial_cash=initial_cash,
        idempotency_key=key,
        assumptions=AssumptionAck(
            profile_sha256=entry.profiles[0].profile_sha256, ids=entry.profiles[0].assumption_ids
        ),
    )
    session = Session(run_id=run_id, request=request, strategy=strategy, entry=entry,
                      strategy_parameters=strategy_parameters or {}, data_binding_sha256=data_binding_sha256)
    return OwnerClient.reopen(store, session)


@dataclass(frozen=True)
class OwnerClient:
    session: Session
    use: object

    @classmethod
    def reopen(cls, store, session):
        verify_runtime()
        validate_binding(session.entry)

        def open_use():
            bound = session.entry.owner_binding
            view = store.m2(
                session.entry.binding.dataset_id,
                profile_sha256=session.entry.binding.use_profile_sha256,
                mode="conditional_research",
            )
            use = view.use(
                bound.owner_window_id,
                assumption_ack=[document(a).to_dict() for a in bound.assumption_refs],
                consumer_binding=owner_binding(session.entry, session.request, session.run_id),
            )
            require(
                use.envelope().data.decode() == bound.envelope,
                "M2_OWNER_BINDING_MISMATCH",
                "Reopened envelope differs",
            )
            return cls(session, use)

        return owner_call(open_use)

    def context(self, current):
        check_context(
            self.session.entry, current, run_id=self.session.run_id, request=self.session.request
        )
        return owner_call(
            lambda: decision_context(self.session.entry, current, self.session.request)
        )

    def previous(self, current, call):
        context = self.context(current)  # before the public data query
        verify_runtime()
        return owner_call(lambda: self.use.decision_prev_close(context, call=call))

    def reauthorize(self, read, origin, current):
        self.context(origin)
        context = self.context(current)
        require(
            origin.event_cursor <= current.event_cursor
            and origin.generation <= current.generation
            and (origin.generation < current.generation or origin.epoch == current.epoch),
            "M2_READ_AUTHORITY_MISMATCH",
            "Owner declarations do not authorize host progression rollback",
        )
        verify_runtime()

        def revalidate():
            refreshed = read.with_authorization(self.use.revalidate(read, context=context))
            require(
                (read.query, read.result, read.sources, read.evidence)
                == (refreshed.query, refreshed.result, refreshed.sources, refreshed.evidence),
                "M2_STABLE_EVIDENCE_MISMATCH",
                "Public reauthorization must preserve all four stable documents",
            )
            return self.use.verify(refreshed, context=context)

        return owner_call(revalidate)

    def engine_context(self, current, *, phase, logical_at):
        self.context(current)
        return owner_call(
            lambda: self.use.context(
                current_date=current.trading_date,
                logical_at=logical_at,
                phase=phase,
                query_end=(
                    datetime.combine(current.previous_open, time(23, 59, 59, 999999), SHANGHAI)
                    if phase in {"INITIALIZE", "DECIDE"}
                    else datetime.combine(current.trading_date, time(15), SHANGHAI)
                ),
                visibility="assumed",
                consumer="engine",
                event_cursor=current.event_cursor,
                restore_generation=current.generation,
                epoch=epoch_number(current.epoch),
            )
        )

    def engine_read(self, operation, context, **parameters):
        allowed = {
            "execution_bar",
            "valuation_bar",
            "trading_state",
            "listing",
            "successor",
            "modeled_actions",
        }
        require(operation in allowed, "M2_ROLE_SCOPE", "Engine-only public operation required")
        verify_runtime()
        return owner_call(lambda: getattr(self.use, operation)(context, **parameters))

    def require_execution(self):
        require_backend_execution()
