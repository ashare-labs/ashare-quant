"""Separate owner facts from explicitly selected AQ research assumptions."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from ashare_quant.execution_six.contracts import Read
from ashare_quant.execution_six.owner import PublicOwner as BaseOwner

from .carry_cases import CASES


class CarryOwner(BaseOwner):
    cases = CASES
    worker_module = "ashare_quant.round_one.carry_worker"

    @staticmethod
    def get_pin():
        return json.loads(Path(__file__).with_name("carry-owner-pin.json").read_text())

    def __init__(self, binding, owner_python, case_id, *, carry_model=None, **unused):
        self.case = CASES[case_id]
        self.model = json.loads(Path(__file__).with_name("carry-model.json").read_text())
        if carry_model != self.case.research_model or carry_model != self.model["schema"]:
            raise RuntimeError("CARRY_EXPLICIT_RESEARCH_MODEL_REQUIRED")
        self.binding = json.loads(Path(binding).read_text())
        self.binding_sha256 = hashlib.sha256(Path(binding).read_bytes()).hexdigest()
        self.data_pin = json.loads(Path(__file__).with_name("carry-data-pin.json").read_text())
        if (self.binding_sha256, self.binding["dataset_id"]) != (
            self.data_pin["market_binding_sha256"],
            self.data_pin["dataset_id"],
        ):
            raise RuntimeError("CARRY_MARKET_BINDING_CHANGED")
        self.python, self.cache, self.queries = str(Path(owner_python).absolute()), {}, None
        self.manifest = self.call(dict(operation="describe"))
        if (
            self.manifest["source_kind"] != "local_jq_backtest_observation"
            or self.manifest["historical_pit"] is not False
            or self.manifest["live_execution_permission"] is not False
        ):
            raise RuntimeError("CARRY_SOURCE_SCOPE_CHANGED")
        self.facts = self.call(
            dict(
                operation="carry",
                window="B22-settlement" if case_id == "B22" else "carry-march",
                security=self.case.security,
            )
        )
        self.links = {r["session_date"]: r for r in self.facts["calendar"]["links"]}
        if tuple(self.links) != self.case.sessions or any(
            r["status"] != "supported" for r in self.links.values()
        ):
            raise RuntimeError("CARRY_BOUND_SESSION_LINKS_REQUIRED")
        for current, following in zip(self.case.sessions, self.case.sessions[1:]):
            if (
                self.links[current]["next_session"] != following
                or self.links[following]["previous_session"] != current
            ):
                raise RuntimeError("CARRY_NONADJACENT_SESSIONS")
        # Known facts may contradict a research hypothesis. Unknowns stay unknown.
        if any(v.get("events") for v in self.facts["actions"].values() if isinstance(v, dict)):
            raise RuntimeError("CARRY_KNOWN_ACTION_REQUIRES_EVENT_MODEL")
        self.bootstraps = [
            self.call(dict(operation="bootstrap", security=self.case.security, day=d))
            for d in self.case.sessions[:2]
        ]

    def descriptor(self):
        return dict(
            dataset_id=self.binding["dataset_id"],
            evidence_kind="real_source_observations",
            real_market_data=True,
            binding_sha256=self.binding_sha256,
            owner_pin=self.get_pin(),
            data_publisher=self.binding["owner"],
            manifest=self.manifest,
            historical_pit=False,
            live_execution_permission=False,
            quality="conditional_sparse_research",
            conditions=self.binding["conditions"],
            gaps=self.binding["gaps"],
            research_model=self.model,
            known_facts=self.facts,
            facts_binding=self.data_pin,
            unknowns=self.facts["required_unknowns"],
            original_case_inputs_only=True,
        )

    def bootstrap(self, case):
        if case != self.case:
            raise RuntimeError("CARRY_CASE_MISMATCH")
        calendar = sorted(
            {
                v
                for row in self.links.values()
                for v in (row["previous_session"], row["session_date"], row["next_session"])
            }
        )
        return dict(
            security=case.security,
            day=str(case.day),
            calendar=calendar,
            execution_sessions=list(case.sessions),
            calendar_links=self.links,
            calendar_kind="owner_bounded_adjacency",
            owner_bootstraps=self.bootstraps,
            research_model=self.model,
            historical_eligibility=None,
            lifecycle_basis="explicit_research_assumption_not_historical_fact",
        )

    def read(self, operation, logical_at, **parameters):
        if logical_at.date().isoformat() not in self.case.sessions:
            raise RuntimeError("CARRY_READ_CLOCK_OUT_OF_SCOPE")
        consumer = parameters.get("consumer", "engine")
        if operation == "lifecycle":
            target = date.fromisoformat(parameters["target_date"])
            if consumer != "engine" or target.isoformat() not in self.case.sessions[:2]:
                raise RuntimeError("CARRY_RESEARCH_IDENTITY_SCOPE_REQUIRED")
            return Read.of(
                operation,
                logical_at,
                dict(
                    listed=True,
                    active=True,
                    delisted=False,
                    basis="explicit_research_assumption",
                    historical_eligibility=None,
                ),
                dict(
                    evidence_kind="explicit_research_assumption",
                    model=self.model["schema"],
                    owner_lifecycle=self.facts["lifecycle"],
                    strategy_visible=False,
                ),
            )
        if operation == "previous_close":
            if consumer != "engine" or logical_at.strftime("%H:%M") != "09:00":
                raise RuntimeError("CARRY_INITIALIZATION_ROLE_REQUIRED")
            previous = self.links[logical_at.date().isoformat()]["previous_session"]
            label = previous + "T00:00:00+08:00"
            public = self.query("daily", logical_at, "initialization", start=label, end=label)
            row = self.one(public)
            if (
                row["strategy_visible"]
                or row["matching_eligible"]
                or set(row["values"]) != {"close"}
            ):
                raise RuntimeError("CARRY_INITIALIZATION_ENVELOPE_INVALID")
            return Read.of(
                operation,
                logical_at,
                dict(close=row["values"]["close"], valuation_date=previous),
                dict(
                    evidence_kind="real_source_observations",
                    public_result=public,
                    consumer="engine",
                    strategy_visible=False,
                ),
            )
        if logical_at.date().isoformat() == "2024-03-06":
            raise RuntimeError("CARRY_CASH_ONLY_SESSION_HAS_NO_MARKET_INPUT")
        bound = copy.copy(self)
        bound.case = replace(self.case, day=logical_at.date())
        return BaseOwner.read(bound, operation, logical_at, **parameters)
