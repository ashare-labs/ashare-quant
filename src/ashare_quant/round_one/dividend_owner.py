"""B26 composite public reads with distinct strategy, initialization and point roles."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ashare_quant.execution_six.contracts import Read, owner_price
from ashare_quant.execution_six.owner import PublicOwner as BaseOwner

from .carry_owner import CarryOwner
from .dividend_cases import CASES
from .premarket import MODEL as PREMARKET_MODEL
from .premarket import admit, collect
from .queries import QueryFacade


def check_known_actions(facts, fact):
    """Only the independently bound cash event may explain observed action rows."""
    expected = facts["actions"]["known_ordinary_cash_schedule"]["events"]
    keys = [
        "security",
        "event_type",
        "announcement_date",
        "record_date",
        "ex_date",
        "payment_date",
        "gross_cash_per_share",
        "bonus_share_ratio",
        "capitalization_share_ratio",
    ]
    if len(expected) != 1 or any(expected[0][k] != fact[k] for k in keys):
        raise RuntimeError("B26_CASH_FACT_CONFLICT")
    for category, entry in facts["actions"].items():
        if not isinstance(entry, dict) or not entry.get("events"):
            continue
        if category == "known_ordinary_cash_schedule":
            continue
        if category == "provider_effective_actions" and all(
            e["category_raw"] == 1 and e["effective_date"] == fact["ex_date"]
            for e in entry["events"]
        ):
            continue
        raise RuntimeError("B26_OTHER_KNOWN_ACTION_REQUIRES_ADAPTER:" + category)


class DividendOwner(CarryOwner):
    worker_module = "ashare_quant.round_one.dividend_worker"

    def __init__(
        self,
        binding,
        owner_python,
        case_id,
        *,
        carry_model=None,
        query_binding=None,
        query_owner_python=None,
        premarket_binding=None,
        premarket_wheel=None,
        **unused,
    ):
        self.case = CASES[case_id]
        if carry_model == PREMARKET_MODEL:
            self.case = replace(self.case, research_model=carry_model)
            model_file = "dividend-premarket-model.json"
        elif carry_model == "aq.b26-jq-fixed-dividend-tax.v1":
            self.case = replace(self.case, research_model=carry_model)
            model_file = "dividend-jq-model.json"
        else:
            model_file = "dividend-research-model.json"
        self.model = json.loads(Path(__file__).with_name(model_file).read_text())
        if carry_model != self.case.research_model or carry_model != self.model["schema"]:
            raise RuntimeError("B26_EXPLICIT_RESEARCH_MODEL_REQUIRED")
        self.premarket_binding, self.premarket_wheel = premarket_binding, premarket_wheel
        if carry_model == PREMARKET_MODEL and not (premarket_binding and premarket_wheel):
            raise RuntimeError("B26_PREMARKET_OWNER_INPUTS_REQUIRED")
        if carry_model != PREMARKET_MODEL and (premarket_binding or premarket_wheel):
            raise RuntimeError("B26_PREMARKET_EXPLICIT_MODEL_REQUIRED")
        self.data_pin = json.loads(Path(__file__).with_name("dividend-data-pin.json").read_text())
        self.binding_sha256 = hashlib.sha256(Path(binding).read_bytes()).hexdigest()
        if self.binding_sha256 != self.data_pin["market"]["sha256"]:
            raise RuntimeError("B26_MARKET_BINDING_CHANGED")
        self.binding = json.loads(Path(binding).read_text())
        self.python, self.cache = str(Path(owner_python).absolute()), {}
        self.manifest = self.call(dict(operation="describe"))
        if self.manifest["historical_pit"] or self.manifest["live_execution_permission"]:
            raise RuntimeError("B26_MARKET_CAPABILITY_CHANGED")
        self.facts = self.call(
            dict(operation="carry", window="B26-known-fact-carry", security=self.case.security)
        )
        self.fact = self.call(dict(operation="cash_fact"))
        check_known_actions(self.facts, self.fact)
        self.links = {r["session_date"]: r for r in self.facts["calendar"]["links"]}
        if tuple(self.links) != self.case.sessions or any(
            r["status"] != "supported" for r in self.links.values()
        ):
            raise RuntimeError("B26_CALENDAR_SCOPE_CHANGED")
        if (
            self.links[self.case.sessions[0]]["next_session"] != self.case.sessions[1]
            or self.links[self.case.sessions[1]]["previous_session"] != self.case.sessions[0]
        ):
            raise RuntimeError("B26_RECORD_EX_NOT_ADJACENT")
        self.bootstraps = [
            self.call(
                dict(operation="bootstrap", security=self.case.security, day=self.case.sessions[0])
            )
        ]
        if not query_binding or not query_owner_python:
            raise RuntimeError("B26_FIXED_QUERY_PROVIDER_REQUIRED")
        if (
            hashlib.sha256(Path(query_binding).read_bytes()).hexdigest()
            != self.data_pin["queries"]["sha256"]
        ):
            raise RuntimeError("B26_QUERY_BINDING_CHANGED")
        self.queries = QueryFacade(query_binding, query_owner_python, "D4")
        self.query_ops = json.loads(Path(__file__).with_name("query-inputs.json").read_text())[
            "cases"
        ]["B26"]["operations"]

    def descriptor(self):
        result = dict(
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
            gaps=self.binding["gaps"],
            research_model=self.model,
            known_facts=self.facts,
            facts_binding=self.data_pin,
            unknowns=self.facts["required_unknowns"],
        )
        result.update(
            cash_fact=self.fact,
            queries=self.queries.descriptor(),
            point_model=self.model["point_valuation"],
            conditions=self.binding.get("conditions", self.manifest["limitations"]),
        )
        if self.model["schema"] == PREMARKET_MODEL:
            result["premarket_owner_pin"] = json.loads(
                Path(__file__).with_name("premarket-owner-pin.json").read_text()
            )
        return result

    def query_operation(self, logical_at):
        choices = [op for op in self.query_ops if op["as_of"] == logical_at.isoformat()]
        if len(choices) != 1:
            raise RuntimeError("B26_QUERY_NOT_AVAILABLE_AT_CLOCK")
        return choices[0]

    def read(self, operation, logical_at, **parameters):
        if logical_at.date().isoformat() not in self.case.sessions:
            raise RuntimeError("B26_READ_OUTSIDE_WINDOW")
        consumer = parameters.get("consumer", "engine")
        if operation == "lifecycle":
            return super().read(operation, logical_at, **parameters)
        if operation in {
            "cash_fact",
            "previous_close",
            "point",
            "valuation",
            "premarket_reference",
        }:
            if consumer != "engine":
                raise RuntimeError("B26_ENGINE_ONLY_ROLE")
            if operation == "premarket_reference":
                if self.model["schema"] != PREMARKET_MODEL:
                    raise RuntimeError("B26_PREMARKET_EXPLICIT_MODEL_REQUIRED")
                if (
                    logical_at.isoformat()
                    != self.model["premarket_reference"]["model_available_at"]
                ):
                    raise RuntimeError("B26_PREMARKET_CLOCK_REQUIRED")
                public = collect(
                    self.python,
                    self.premarket_binding,
                    self.premarket_wheel,
                    self.case.security,
                    logical_at.isoformat(),
                )
                value = dict(
                    price=str(
                        admit(
                            public,
                            security=self.case.security,
                            as_of=logical_at.isoformat(),
                            fact=self.fact,
                            raw_previous_close=parameters["raw_previous_close"],
                        )
                    )
                )
            elif operation == "cash_fact":
                value, public = self.fact, self.fact
            elif operation == "previous_close":
                if logical_at.isoformat() != "2024-06-14T09:00:00+08:00":
                    raise RuntimeError("B26_INITIALIZATION_CLOCK_REQUIRED")
                public = self.call(
                    dict(
                        operation="initialization",
                        parameters=dict(
                            query_id=self.data_pin["initialization_query_id"],
                            security=self.case.security,
                            valuation_date="2024-06-13",
                            as_of=logical_at.isoformat(),
                        ),
                    )
                )
                if (
                    public["status"] != "available"
                    or public["strategy_visible"]
                    or public["matching_eligible"]
                    or public["current_quote"]
                    or datetime.fromisoformat(public["available_at"]) > logical_at
                    or len(public["rows"]) != 1
                    or set(public["rows"][0]["values"]) != {"close"}
                ):
                    raise RuntimeError("B26_INITIALIZATION_ENVELOPE_INVALID")
                value = dict(
                    close=public["rows"][0]["values"]["close"], valuation_date="2024-06-13"
                )
            elif operation == "point":
                if logical_at.isoformat() != "2024-06-14T10:00:00+08:00":
                    raise RuntimeError("B26_POINT_CLOCK_REQUIRED")
                public = self.call(
                    dict(
                        operation="point",
                        parameters=dict(
                            security=self.case.security,
                            at=logical_at.isoformat(),
                            role="retrospective_point_research",
                        ),
                    )
                )
                if (
                    public["status"] != "available"
                    or public["historical_pit"]
                    or public["strategy_visible"]
                    or len(public["rows"]) != 1
                ):
                    raise RuntimeError("B26_POINT_SCOPE_INVALID")
                row = public["rows"][0]
                if (
                    row["security"] != self.case.security
                    or row["source_label_at"] != logical_at.isoformat()
                    or row["strategy_visible"]
                ):
                    raise RuntimeError("B26_POINT_LABEL_MISMATCH")
                value = dict(price=row["values"]["price"])
                owner_price(value["price"])
            else:
                # Terminal values keep the last admitted daily mark; this never
                # permits a same-day close before the 15:30 observation.
                at = logical_at.replace(hour=15, minute=30, second=0, microsecond=0)
                if logical_at < at:
                    raise RuntimeError("B26_DAILY_CLOSE_NOT_YET_OBSERVED")
                op = self.query_operation(at)
                public = self.queries.query(
                    **{k: op[k] for k in ("api", "parameters", "as_of", "role")}
                )
                if len(public["rows"]) != 1 or public["matching_eligible"]:
                    raise RuntimeError("B26_DAILY_VALUATION_ENVELOPE_INVALID")
                value = dict(close=public["rows"][0]["close"])
            return Read.of(
                operation,
                logical_at,
                value,
                dict(
                    public_result=public,
                    consumer="engine",
                    strategy_visible=False,
                    historical_pit=False,
                    model=self.model["schema"],
                    evidence_kind="real_source_observations",
                ),
            )
        if logical_at.date() != self.case.day:
            raise RuntimeError("B26_NO_EX_DAY_STRATEGY_QUOTE_OR_BAR")
        bound = copy.copy(self)
        bound.case = replace(self.case, day=logical_at.date())
        return BaseOwner.read(bound, operation, logical_at, **parameters)
