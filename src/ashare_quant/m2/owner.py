"""Read-only fixed owner preparation using public APIs, never raw source files."""

from dataclasses import dataclass
from datetime import datetime, time

from ashare_data import M2ConsumerBinding
from ashare_data.m2_types import canonical_hash

from ashare_quant.contracts import SHANGHAI, DomainError

from .catalogue import PreparationEntry, check_context
from .common import digest, require
from .contracts import (
    AssumptionAck,
    BacktestRequest,
    DataBinding,
    DayScope,
    ModelProfile,
    WindowPlan,
)
from .journal import Session
from .owner_contracts import OwnerBinding
from .owner_wire import (
    RELEASE_SHA,
    REVIEW_SHA,
    decision_context,
    document,
    epoch_number,
    owner_binding,
    owner_call,
    validate_binding,
    verify_runtime,
)


def require_backend_execution():
    raise DomainError("M2_NATIVE_INTEGRATION_PENDING", "Owner codec preparation does not authorize native execution", 422)


def prepare(store, strategy, *, run_id, initial_cash, key, profile_sha256, window_id):
    """Explicit candidate data reads only. Caller supplies an already registered strategy."""
    pin = verify_runtime()
    allowed = next((w for w in pin["windows"] if w["window_id"] == window_id), None)
    require(allowed is not None, "M2_WINDOW_NOT_REGISTERED", "Select a fixed owner window")
    def build():
        view = store.m2(pin["dataset_id"], profile_sha256=profile_sha256, mode="conditional_research")
        descriptor = view.descriptor()
        desc = descriptor.to_dict()
        require(desc in pin["owner_pins"] and descriptor.sha256 == allowed["owner_pin_sha256"],
                "M2_OWNER_BINDING_MISMATCH", "Explicit owner profile does not contain the selected window")
        plan_doc = next((w for w in view.windows() if w.to_dict()["geometry"]["window_id"] == window_id), None)
        require(plan_doc is not None, "M2_WINDOW_NOT_REGISTERED", "Owner window is absent")
        plan = plan_doc.to_dict()
        assumptions = view.assumptions(window_id)
        binding = DataBinding(owner_version=pin["version"], owner_commit=pin["commit"], owner_wheel_sha256=pin["wheel"]["sha256"],
            dataset_id=desc["dataset_id"], manifest_sha256=desc["manifest_sha256"], use_profile_sha256=profile_sha256,
            query_contract_sha256=desc["query_contract_sha256"], independent_review_sha256=REVIEW_SHA,
            backend_policy_sha256=digest({"policy": "m2.owner_codec_reviewed.v1", "release": RELEASE_SHA, "review": REVIEW_SHA,
                "owner_pin": descriptor.sha256, "window_plan": plan["window_plan_sha256"], "envelope": allowed["envelope_sha256"]}))
        window = WindowPlan(days=tuple(DayScope(trading_date=x["current"], previous_open=x["previous_open"],
            next_open=x["next_open"], owner_evidence_sha256=canonical_hash({"plan": plan["window_plan_sha256"], "link": x}))
            for x in plan["geometry"]["calendar_links"]), owner_calendar_sha256=plan["window_spec_sha256"],
            read_domain_sha256=canonical_hash(plan["read_roles"]))
        model = ModelProfile(binding_id=binding.binding_id, window_id=window.window_id, owner_use_profile_sha256=profile_sha256,
            visibility_policy_sha256=canonical_hash([a.to_dict() for a in assumptions]),
            matching_policy_sha256=digest("m2.A-CLOSE.current_close.conditional.v1"),
            fee_policy_sha256=digest("m2.A-COST.rq641.fixed.v1"), pool_policy_sha256=digest("m2.A-POOL.600000.fixed.v1"))
        request = BacktestRequest(strategy_version_id=strategy.version_id, data_binding_id=binding.binding_id,
            window_id=window.window_id, initial_cash=initial_cash, idempotency_key=key,
            assumptions=AssumptionAck(profile_sha256=model.profile_sha256, ids=model.assumption_ids))
        consumer = M2ConsumerBinding(owner_pin_sha256=descriptor.sha256, window_id=window_id,
            window_plan_sha256=plan["window_plan_sha256"], run_binding_sha256=binding.binding_id, run_id=run_id,
            consumer_request_sha256=digest(request), strategy_version_id=strategy.version_id,
            assumption_ack_bundle_sha256=model.profile_sha256)
        use = view.use(window_id, assumption_ack=[a.to_dict() for a in assumptions], consumer_binding=consumer)
        envelope = use.envelope()
        bound = OwnerBinding(release_sha256=RELEASE_SHA, descriptor=descriptor.data.decode(), window_plan=plan_doc.data.decode(),
            envelope=envelope.data.decode(), assumption_refs=tuple(a.data.decode() for a in assumptions), owner_window_id=window_id)
        quality = view.quality().data.decode()
        entry = PreparationEntry(binding=binding, windows=(window,), profiles=(model,), evidence_kind="observed", owner_binding=bound,
            original_unknowns=(quality, "source_claim_unverified", "conditional owner API/codec independently accepted; native admission closed"))
        validate_binding(entry)
        session = Session(run_id=run_id, request=request, strategy=strategy, entry=entry)
        return OwnerClient(session, use)
    return owner_call(build)


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
            view = store.m2(session.entry.binding.dataset_id, profile_sha256=session.entry.binding.use_profile_sha256,
                            mode="conditional_research")
            use = view.use(bound.owner_window_id, assumption_ack=[document(a).to_dict() for a in bound.assumption_refs],
                           consumer_binding=owner_binding(session.entry, session.request, session.run_id))
            require(use.envelope().data.decode() == bound.envelope, "M2_OWNER_BINDING_MISMATCH", "Reopened envelope differs")
            return cls(session, use)
        return owner_call(open_use)

    def context(self, current):
        check_context(self.session.entry, current, run_id=self.session.run_id, request=self.session.request)
        return owner_call(lambda: decision_context(self.session.entry, current, self.session.request))

    def previous(self, current, call):
        context = self.context(current)  # before the public data query
        verify_runtime()
        return owner_call(lambda: self.use.decision_prev_close(context, call=call))

    def reauthorize(self, read, origin, current):
        self.context(origin)
        context = self.context(current)
        require(origin.event_cursor <= current.event_cursor and origin.generation <= current.generation
                and (origin.generation < current.generation or origin.epoch == current.epoch),
                "M2_READ_AUTHORITY_MISMATCH", "Owner declarations do not authorize host progression rollback")
        verify_runtime()
        def revalidate():
            refreshed = read.with_authorization(self.use.revalidate(read, context=context))
            require((read.query, read.result, read.sources, read.evidence) ==
                    (refreshed.query, refreshed.result, refreshed.sources, refreshed.evidence),
                    "M2_STABLE_EVIDENCE_MISMATCH", "Public reauthorization must preserve all four stable documents")
            return self.use.verify(refreshed, context=context)
        return owner_call(revalidate)

    def engine_context(self, current, *, phase, logical_at):
        self.context(current)
        return owner_call(lambda: self.use.context(current_date=current.trading_date, logical_at=logical_at, phase=phase,
            query_end=(datetime.combine(current.previous_open, time(23, 59, 59, 999999), SHANGHAI)
                if phase in {"INITIALIZE", "DECIDE"} else datetime.combine(current.trading_date, time(15), SHANGHAI)),
            visibility="assumed", consumer="engine",
            event_cursor=current.event_cursor, restore_generation=current.generation, epoch=epoch_number(current.epoch)))

    def engine_read(self, operation, context, **parameters):
        allowed = {"execution_bar", "valuation_bar", "trading_state", "listing", "successor", "modeled_actions"}
        require(operation in allowed, "M2_ROLE_SCOPE", "Engine-only public operation required")
        verify_runtime()
        return owner_call(lambda: getattr(self.use, operation)(context, **parameters))

    def require_execution(self):
        require_backend_execution()
