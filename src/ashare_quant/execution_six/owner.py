"""Thin public A数达 projection; never read raw market files or answer artifacts."""

import hashlib
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

from ashare_quant.diagnostics.runner import digest

from .contracts import CASES, Read


def pin():
    return json.loads(Path(__file__).with_name("owner-pin.json").read_text())


def share_count(value):
    if type(value) is not str:
        raise ValueError("SIX_OWNER_SHARE_COUNT_TYPE")
    value = Decimal(value)
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise ValueError("SIX_OWNER_SHARE_COUNT_VALUE")
    return int(value)


class PublicOwner:
    cases = CASES
    get_pin = staticmethod(pin)
    dataset_pin_path = Path(__file__).with_name("dataset-pin.json")
    worker_module = "ashare_quant.execution_six.owner_worker"

    def __init__(self, binding, owner_python, case_id):
        self.case = self.cases[case_id]
        self.binding = json.loads(Path(binding).read_text())
        self.binding_sha256 = hashlib.sha256(Path(binding).read_bytes()).hexdigest()
        self.python = str(Path(owner_python).absolute())
        self.cache = {}
        data_pin = json.loads(self.dataset_pin_path.read_text())
        if any(self.binding[k] != data_pin[k] for k in ("dataset_id", "request_sha256")):
            raise RuntimeError("SIX_DATASET_PIN_MISMATCH")
        if any(self.binding["owner"][k] != self.get_pin()[k] for k in ("version", "commit", "wheel_sha256")):
            raise RuntimeError("SIX_OWNER_DELIVERY_PIN_MISMATCH")
        self.validate_scope()
        self.manifest = self.call({"operation": "describe"})
        if self.manifest["sources"] != data_pin["sources"]:
            raise RuntimeError("SIX_SOURCE_BINDINGS_MISMATCH")
        if (self.manifest["source_kind"] != "local_jq_backtest_observation"
                or self.manifest["local_research_use"] != "conditional_sparse_native_execution"
                or self.manifest["historical_pit"] is not False
                or self.manifest["live_execution_permission"] is not False):
            raise RuntimeError("SIX_REAL_OBSERVATION_BINDING_REQUIRED")

    def validate_scope(self):
        if self.binding["case_scope"][self.case.case_id] != [self.case.security, str(self.case.day)]:
            raise RuntimeError("SIX_OWNER_DELIVERY_SCOPE_MISMATCH")

    def call(self, query):
        key = digest(query)
        if key in self.cache:
            return json.loads(self.cache[key])
        package_root = str(Path(__file__).resolve().parents[1])
        # Bind only AQ's namespace. Inserting its entire installed site-packages
        # would accidentally shadow the separately pinned owner's dependencies.
        boot = ("import importlib.util,sys;"
                "s=importlib.util.spec_from_file_location('ashare_quant',sys.argv[1]+'/__init__.py',"
                "submodule_search_locations=[sys.argv[1]]);"
                "m=importlib.util.module_from_spec(s);sys.modules['ashare_quant']=m;s.loader.exec_module(m);"
                f"from {self.worker_module} import main;main()")
        payload = {"store": self.binding["store_path"], "dataset_id": self.binding["dataset_id"], "query": query}
        env = {"PATH": os.defpath, "TZ": "UTC", "OPENBLAS_NUM_THREADS": "1",
               "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
        process = subprocess.run([self.python, "-I", "-B", "-c", boot, package_root],
                                 input=json.dumps(payload), text=True, capture_output=True,
                                 cwd="/", env=env, timeout=30)
        reply = json.loads(process.stdout)
        if process.returncode:
            raise RuntimeError("SIX_OWNER_READ_REFUSED:" + json.dumps(reply.get("error")))
        if (reply["query_sha256"], reply["content_sha256"], reply["dataset_id"], reply["network_used"]) != (
                key, self.get_pin()["content_sha256"], self.binding["dataset_id"], False):
            raise RuntimeError("SIX_OWNER_REPLY_IDENTITY_MISMATCH")
        self.cache[key] = json.dumps(reply["public_result"])
        return json.loads(self.cache[key])

    def descriptor(self):
        return {"dataset_id": self.binding["dataset_id"], "evidence_kind": "real_source_observations",
                "real_market_data": True, "binding_sha256": self.binding_sha256,
                "owner_pin": self.get_pin(), "manifest": self.manifest,
                "historical_pit": False, "live_execution_permission": False,
                "quality": "conditional_sparse_research", "conditions": self.binding["conditions"],
                "gaps": self.binding["gaps"]}

    def bootstrap(self, case):
        if case != self.case:
            raise RuntimeError("SIX_OWNER_CASE_MISMATCH")
        public = self.call({"operation": "bootstrap", "security": case.security, "day": str(case.day)})
        model = public["conditional_model"]
        if (model["state"], model["round_lot"], model["tick_size"], model["session_dates"]) != (
                "explicit_research_assumption_not_market_fact", 100, "0.01", [str(case.day)]):
            raise RuntimeError("SIX_OWNER_INITIALIZATION_MODEL_MISMATCH")
        return {"security": case.security, "day": str(case.day), "calendar": model["session_dates"],
                "calendar_kind": "single_session_model_not_exchange_calendar", "owner_bootstrap": public,
                "lifecycle_model": "assume_instrument_active_for_bound_session_only",
                "consumer_terminal_model": "no_terminal_delisting_adjustment_in_single_session",
                "overnight_eligibility": None, "next_session": None}

    def query(self, kind, at, role, **bounds):
        return self.call({"operation": "read", "parameters": {"security": self.case.security,
                         "kind": kind, "as_of": at.isoformat(), "role": role, **bounds}})

    @staticmethod
    def one(public):
        if public["status"] != "available" or len(public["rows"]) != 1:
            raise RuntimeError("SIX_OWNER_REQUIRED_OBSERVATION_UNAVAILABLE:" + str(public["kind"]) + ":" + str(public["reason"]))
        return public["rows"][0]

    def read(self, operation, logical_at, **parameters):
        consumer = parameters.get("consumer", "engine")
        if consumer not in {"engine", "strategy"} or (consumer == "strategy" and operation not in {"quote", "history", "auction"}):
            raise RuntimeError("SIX_OWNER_ROLE_DENIED")
        if logical_at.date() != self.case.day:
            raise RuntimeError("SIX_OWNER_CLOCK_OUT_OF_SCOPE")
        if operation == "lifecycle":
            if parameters["target_date"] != str(self.case.day):
                raise RuntimeError("SIX_OWNER_LIFECYCLE_DATE_UNKNOWN")
            public = self.bootstrap(self.case)
            value = {"listed": True, "active": True, "delisted": False,
                     "basis": public["lifecycle_model"], "historical_eligibility": None}
        elif operation == "quote":
            role = "strategy" if consumer == "strategy" else ("early_order" if logical_at.hour == 9 and logical_at.minute < 30 else "matching")
            public = self.query("current", logical_at, role)
            row = self.one(public)
            v = row["values"]
            value = {"last": v["last_price"], "day_open": v["day_open"], "upper_limit": v["high_limit"],
                     "lower_limit": v["low_limit"], "suspended": v["paused"], "is_st": v["is_st"],
                     "executable": row["executable"], "quality": row["quality"]}
        elif operation == "bar":
            public = self.query("minute", logical_at, "matching", start=logical_at.isoformat(), end=logical_at.isoformat())
            row = self.one(public)
            value = {**row["values"], "at": row["source_label_at"],
                     "volume": share_count(row["values"]["volume"]),
                     "executable": row["executable"], "quality": row["quality"]}
            if row["executable"] is False and value["volume"] > 0:
                raise RuntimeError("SIX_OWNER_BAR_NOT_EXECUTABLE")
        elif operation == "history":
            public = self.query("minute", logical_at, "strategy", end=logical_at.isoformat())
            count = parameters.get("count", 4)
            value = {**public, "rows": sorted(public["rows"], key=lambda r: r["source_label_at"])[-count:]}
        elif operation == "auction":
            public = value = self.query("auction", logical_at, "strategy")
        elif operation == "valuation":
            public = self.query("daily", logical_at, "final_valuation")
            value = {"close": self.one(public)["values"]["close"]}
        elif operation == "capacity":
            if self.case.case_id not in {"B10", "B13"}:
                raise RuntimeError("SIX_CAPACITY_MODEL_OUT_OF_SCOPE")
            public = self.query("daily", logical_at, "engine_capacity")
            row = self.one(public)
            if not public["retrospective"] or row["strategy_visible"] or set(row["values"]) != {"volume"}:
                raise RuntimeError("SIX_CAPACITY_ROLE_MISMATCH")
            value = {"volume": share_count(row["values"]["volume"]), "basis": "retrospective_whole_day"}
        elif operation in {"opening_price", "opening_volume"}:
            if self.case.case_id != "B12" or logical_at.strftime("%H:%M:%S") != "09:30:00":
                raise RuntimeError("SIX_OPENING_MODEL_OUT_OF_SCOPE")
            public = self.query("current" if operation == "opening_price" else "auction", logical_at, "opening_observation")
            row = self.one(public)
            if row["executable"] is False:
                raise RuntimeError("SIX_OPENING_SOURCE_NOT_EXECUTABLE")
            value = ({"price": row["values"]["last_price"], "basis": "observed_0930_current_last"}
                     if operation == "opening_price" else
                     {"volume": share_count(row["values"]["volume"]), "basis": "earlier_visible_auction_volume_model"})
        else:
            raise RuntimeError("SIX_OWNER_OPERATION_UNAVAILABLE:" + operation)
        return Read.of(operation, logical_at, value,
                       {"evidence_kind": "real_source_observations", "public_result": public,
                        "owner_content_sha256": self.get_pin()["content_sha256"], "consumer": consumer})
