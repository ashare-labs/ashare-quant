"""Exact request facade for observed AD query products, not a market data store."""

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from ashare_quant.diagnostics.runner import digest


class QueryRefused(RuntimeError):
    def __init__(self, code, message="", *, origin="ashare-quant", details=None):
        self.code, self.origin, self.details = code, origin, details
        super().__init__(message or code)

    def to_dict(self):
        return dict(code=self.code, message=str(self), origin=self.origin, details=self.details)


class QueryFacade:
    def __init__(self, binding, owner_python, group):
        self.binding = json.loads(Path(binding).read_text())
        self.binding_sha256 = hashlib.sha256(Path(binding).read_bytes()).hexdigest()
        self.pin = json.loads(Path(__file__).with_name("query-owner-pin.json").read_text())
        data_pin = json.loads(Path(__file__).with_name("query-data-pin.json").read_text())
        if any(
            self.binding["owner"][k] != self.pin[k] for k in ("version", "commit", "wheel_sha256")
        ):
            raise QueryRefused("QUERY_OWNER_DELIVERY_PIN_MISMATCH")
        if self.binding["request_sha256"] != data_pin["request_sha256"]:
            raise QueryRefused("QUERY_REQUEST_PIN_MISMATCH")
        self.dataset_id = self.binding["groups"][group]["dataset_id"]
        if self.dataset_id != data_pin["groups"][group]["dataset_id"]:
            raise QueryRefused("QUERY_DATASET_PIN_MISMATCH")
        self.python = str(Path(owner_python).absolute())
        self.manifest = self._call({"operation": "describe"})
        if digest(self.manifest) != data_pin["groups"][group]["manifest_sha256"]:
            raise QueryRefused("QUERY_OWNER_MANIFEST_CHANGED")

    def _call(self, query):
        package_root = str(Path(__file__).resolve().parents[1])
        boot = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('ashare_quant',sys.argv[1]+'/__init__.py',"
            "submodule_search_locations=[sys.argv[1]]);"
            "m=importlib.util.module_from_spec(s);sys.modules['ashare_quant']=m;s.loader.exec_module(m);"
            "from ashare_quant.round_one.query_worker import main;main()"
        )
        payload = {
            "store": self.binding["store_path"],
            "dataset_id": self.dataset_id,
            "query": query,
        }
        env = {
            "PATH": os.defpath,
            "TZ": "UTC",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
        p = subprocess.run(
            [self.python, "-I", "-B", "-c", boot, package_root],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd="/",
            env=env,
            timeout=30,
        )
        reply = json.loads(p.stdout)
        if p.returncode:
            raise QueryRefused("QUERY_TRANSPORT_FAILED", json.dumps(reply.get("error")))
        if (
            reply["query_sha256"],
            reply["content_sha256"],
            reply["dataset_id"],
            reply["network_used"],
        ) != (digest(query), self.pin["content_sha256"], self.dataset_id, False):
            raise QueryRefused("QUERY_REPLY_IDENTITY_MISMATCH")
        if reply["error"] is not None:
            raise QueryRefused(**reply["error"])
        return reply["public_result"]

    def query(self, api, parameters, *, as_of, role, require_historical_pit=False):
        at = datetime.fromisoformat(as_of)
        if at.tzinfo is None or at.utcoffset() is None:
            raise QueryRefused("QUERY_TIMEZONE_REQUIRED")
        if role not in {"audit", "conditional_research"}:
            raise QueryRefused("QUERY_ROLE_DENIED")
        if require_historical_pit:
            raise QueryRefused("QUERY_HISTORICAL_PIT_NOT_CERTIFIED")
        if api in {"get_all_securities", "get_security_info"} and role != "audit":
            raise QueryRefused("QUERY_METADATA_AUDIT_ONLY")
        # The owner validates fields, future bounds and exact request identity.
        # No expected result, stored exception or query inventory is dispatched.
        value = self._call(
            {
                "operation": "query",
                "parameters": {
                    "api": api,
                    "parameters": parameters,
                    "as_of": as_of,
                    "observed_at": as_of,
                },
            }
        )
        if value["status"] != "available":
            raise QueryRefused("QUERY_OBSERVATION_UNAVAILABLE", str(value["reason"]))
        if (value["historical_pit"], value["matching_eligible"], value["network_used"]) != (
            False,
            False,
            False,
        ):
            raise QueryRefused("QUERY_CAPABILITY_ENVELOPE_CHANGED")
        return value

    def descriptor(self):
        return dict(
            dataset_id=self.dataset_id,
            binding_sha256=self.binding_sha256,
            owner_pin=self.pin,
            owner_manifest=self.manifest,
            historical_pit=False,
            matching_eligible=False,
            live_execution_permission=False,
        )
