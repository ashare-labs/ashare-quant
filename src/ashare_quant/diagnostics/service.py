import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ashare_quant import __version__
from ashare_quant.contracts import Contract, DomainError
from ashare_quant.diagnostics.contracts import (
    Alias,
    DiagnosticCapabilities,
    DiagnosticQuery,
    DiagnosticResult,
    OwnerIdentity,
    OwnerRead,
    Sha,
)
from ashare_quant.identity import source_sha256


class SourceConfiguration(Contract):
    python: str
    store: str
    calendar_capture_id: Sha


def pin():
    from ashare_quant.providers.runtime import pin as owner_pin
    from ashare_quant.runtime import runtime_fingerprint
    p = owner_pin()
    return {**p, "content_sha256": digest(p["files"]), "runtime": runtime_fingerprint()}


def identity():
    p = pin()
    return OwnerIdentity(**{k: p[k] for k in ("version", "commit", "wheel_sha256", "content_sha256")})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class Diagnostics:
    def __init__(self, configuration=None):
        raw = configuration if configuration is not None else json.loads(os.getenv("ASHARE_QUANT_DIAGNOSTICS", "{}"))
        self.sources = TypeAdapter(dict[Alias, SourceConfiguration]).validate_python(raw)

    def capabilities(self):
        return DiagnosticCapabilities(configured_sources=tuple(sorted(self.sources)), research_owner=identity())

    def diagnose(self, request: DiagnosticQuery):
        if request.source_alias not in self.sources:
            raise DomainError("DIAGNOSTIC_SOURCE_NOT_CONFIGURED", "Configure an explicit local diagnostics source alias", 422)
        cfg = self.sources[request.source_alias]
        python, store = Path(cfg.python).expanduser().absolute(), Path(cfg.store).expanduser().resolve()
        if not python.is_file() or not os.access(python, os.X_OK) or not store.is_dir():
            raise DomainError("DIAGNOSTIC_RUNTIME_UNAVAILABLE", "Use this environment Python and an existing Store", 503)
        from ashare_quant.providers.runtime import installed_paths
        installed_paths(python, Path(str(files("ashare_data"))).parent)
        query = request.model_dump(mode="json")
        payload = {"pin": pin(), "query": query, "store": str(store), "capture_id": cfg.calendar_capture_id}
        runner = str(files("ashare_quant.diagnostics").joinpath("runner.py"))
        env = {"PATH": os.defpath, "PYTHONHASHSEED": "0", "TZ": "UTC", "OPENBLAS_NUM_THREADS": "1",
               "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
        try:
            result = subprocess.run([str(python), "-I", "-B", runner], input=json.dumps(payload),
                                    text=True, capture_output=True, timeout=30, cwd="/", env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError("DIAGNOSTIC_READER_UNAVAILABLE", "Isolated owner reader did not complete", 503,
                              details={"failure": type(exc).__name__}) from exc
        try:
            reply = json.loads(result.stdout)
            if result.returncode != 0:
                raise DomainError("DIAGNOSTIC_RUNTIME_MISMATCH", "Fixed read-only owner reader refused this environment", 503,
                                  details=reply.get("error"))
            if (reply["protocol"], reply["query_sha256"], reply["capture_id"], reply["content_sha256"], reply["network_used"]) != (
                "aq.owner-market-reader.v1", digest(query), cfg.calendar_capture_id, pin()["content_sha256"], False
            ):
                raise ValueError("Reader identity/request differs")
            calendar, links = OwnerRead.model_validate(reply["calendar"]), OwnerRead.model_validate(reply["calendar_links"])
            preflight = OwnerRead.model_validate(reply["research_preflight"])
            complete = None
            if preflight.status == "readable":
                public = preflight.public_result
                complete = public["research_values_complete"]
                if (type(complete) is not bool or public["execution_permission"] is not False
                        or public["historical_pit"] is not False or public["network_used"] is not False
                        or public["execution_admission"] != "BLOCKED_NO_OWNER_PLAN"):
                    raise ValueError("Unexpected owner preflight boundary")
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise DomainError("DIAGNOSTIC_PROTOCOL_MISMATCH", "Invalid response from fixed owner reader", 503) from exc
        return DiagnosticResult(query=request, query_sha256=digest(query), calendar_capture_id=cfg.calendar_capture_id,
                                data_owner=identity(), calendar=calendar, calendar_links=links,
                                backend_version=__version__, backend_source_sha256=source_sha256(),
                                research_preflight=preflight, research_values_complete=complete,
                                research_readable=("complete_research_values" if complete else "partial_research_values")
                                if complete is not None else ("calendar_only" if calendar.status == "readable" else "blocked"),
                                read_completed_at=datetime.now(timezone.utc))
