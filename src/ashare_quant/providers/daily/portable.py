"""Prepare path-independent metadata exclusively through the public data-owner API."""

from ashare_data import PortableConsumerBinding
from ashare_data.research.portable_types import canonical_hash
from pydantic import Field

from ashare_quant.backtests.daily.catalogue import PreparationEntry
from ashare_quant.backtests.daily.contracts import (
    DataBinding,
    DayScope,
    ModelProfile,
    Sha,
    WindowPlan,
)
from ashare_quant.contracts import Contract
from ashare_quant.providers.daily.owner_contracts import OwnerBinding
from ashare_quant.providers.daily.owner_wire import owner_call, validate_binding, verify_runtime
from ashare_quant.providers.daily.portable_policy import DAYS, POLICY_SHA, validate_plan
from ashare_quant.storage.canonical import digest, require


class LocalDataBinding(Contract):
    schema_version: str = "m2.local-data-binding.v1"
    dataset_id: Sha
    policy_sha256: Sha = POLICY_SHA
    entries: tuple[PreparationEntry, ...] = Field(min_length=1, max_length=3)

    @property
    def identity(self):
        return digest(self)

    def checked(self):
        require(
            self.schema_version == "m2.local-data-binding.v1" and self.policy_sha256 == POLICY_SHA,
            "M2_DATA_BINDING_CHANGED",
            "Different local admission policy",
        )
        names = []
        for entry in self.entries:
            validate_binding(entry)
            require(
                entry.binding.dataset_id == self.dataset_id,
                "M2_DATA_BINDING_CHANGED",
                "Every window must belong to the selected dataset",
            )
            names.append(entry.owner_binding.owner_window_id)
        require(
            len(names) == len(set(names)) and names == sorted(names),
            "M2_DATA_BINDING_CHANGED",
            "Exactly one owner profile for each selected window",
        )
        return self

    def entry(self, window_id):
        self.checked()
        found = next(
            (e for e in self.entries if e.owner_binding.owner_window_id == window_id), None
        )
        require(
            found is not None,
            "M2_WINDOW_NOT_REGISTERED",
            "Selected dataset does not supply this window",
        )
        return found


def bind_dataset(store, dataset_id):
    pin = verify_runtime()

    def build():
        entries = []
        for profile_doc in store.portable_profiles(dataset_id):
            profile_sha = profile_doc.to_dict()["profile_sha256"]
            view = store.portable(dataset_id, profile_sha256=profile_sha, mode="conditional_research")
            descriptor = view.descriptor()
            desc = descriptor.to_dict()
            require(
                desc["dataset_id"] == dataset_id and desc["profile_sha256"] == profile_sha,
                "M2_OWNER_BINDING_MISMATCH",
                "Explicit dataset/profile must match owner descriptor",
            )
            for plan_doc in view.windows():
                plan = plan_doc.to_dict()
                window_id = validate_plan(plan)
                assumptions = view.assumptions(window_id)
                # Metadata-only use: no price query, native account, job or read authority.
                # The owner's envelope is independent of consumer identity. Actual execution
                # opens a new use bound to the accepted strategy/request/run below.
                metadata_id = digest({"purpose": "metadata_only", "owner": desc, "plan": plan})
                consumer = PortableConsumerBinding(
                    owner_pin_sha256=descriptor.sha256,
                    window_id=window_id,
                    window_plan_sha256=plan["window_plan_sha256"],
                    run_binding_sha256=metadata_id,
                    run_id="metadata-" + metadata_id[:24],
                    consumer_request_sha256=metadata_id,
                    strategy_version_id=metadata_id,
                    assumption_ack_bundle_sha256=canonical_hash([a.to_dict() for a in assumptions]),
                )
                use = view.use(
                    window_id,
                    assumption_ack=[a.to_dict() for a in assumptions],
                    consumer_binding=consumer,
                )
                envelope = use.envelope()
                bound = OwnerBinding(
                    policy_sha256=POLICY_SHA,
                    owner_runtime_sha256=pin["runtime_code_sha256"],
                    descriptor=descriptor.data.decode(),
                    window_plan=plan_doc.data.decode(),
                    envelope=envelope.data.decode(),
                    assumption_refs=tuple(a.data.decode() for a in assumptions),
                    owner_window_id=window_id,
                )
                binding = DataBinding(
                    owner_version=pin["version"],
                    owner_commit=pin["commit"],
                    owner_runtime_sha256=pin["runtime_code_sha256"],
                    owner_wheel_sha256=None,
                    dataset_id=dataset_id,
                    manifest_sha256=desc["manifest_sha256"],
                    use_profile_sha256=profile_sha,
                    query_contract_sha256=desc["query_contract_sha256"],
                    independent_review_sha256=pin["contract_review"]["receipt_sha256"],
                    backend_policy_sha256=digest(
                        {"policy": POLICY_SHA, "owner_binding": bound.identity}
                    ),
                )
                window = WindowPlan(
                    days=tuple(
                        DayScope(
                            trading_date=x["current"],
                            previous_open=x["previous_open"],
                            next_open=x["next_open"],
                            owner_evidence_sha256=canonical_hash(
                                {"plan": plan["window_plan_sha256"], "link": x}
                            ),
                        )
                        for x in plan["geometry"]["calendar_links"]
                    ),
                    owner_calendar_sha256=plan["window_spec_sha256"],
                    read_domain_sha256=canonical_hash(plan["read_roles"]),
                )
                model = ModelProfile(
                    binding_id=binding.binding_id,
                    window_id=window.window_id,
                    owner_use_profile_sha256=profile_sha,
                    visibility_policy_sha256=canonical_hash([a.to_dict() for a in assumptions]),
                    matching_policy_sha256=digest("m2.A-CLOSE.current_close.conditional.v1"),
                    fee_policy_sha256=digest("m2.A-COST.rq641.fixed.v1"),
                    pool_policy_sha256=digest("m2.A-POOL.600000.fixed.v1"),
                )
                quality = view.quality().data.decode()
                entry = PreparationEntry(
                    binding=binding,
                    windows=(window,),
                    profiles=(model,),
                    evidence_kind="observed",
                    owner_binding=bound,
                    original_unknowns=(
                        quality,
                        "source_claim_unverified",
                        "new data producer source, local binding and execution not independently reviewed",
                    ),
                )
                validate_binding(entry)
                entries.append(entry)
        require(
            entries and len(entries) <= len(DAYS),
            "M2_WINDOW_NOT_REGISTERED",
            "No bounded owner windows are available",
        )
        return LocalDataBinding(
            dataset_id=dataset_id,
            entries=tuple(sorted(entries, key=lambda e: e.owner_binding.owner_window_id)),
        ).checked()

    return owner_call(build)
