"""Non-executing descriptor, source and parameter checks for a finite local API."""

import ast
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from ashare_quant.contracts import Contract, DomainError

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
SOURCE_LIMIT = 65536
PARAMETER_LIMIT = 8192
OUTPUT_LIMIT = 1048576
PHASE_SECONDS = 5


class PythonDescriptor(Contract):
    schema_version: Literal["trusted-python-descriptor.v1"] = "trusted-python-descriptor.v1"
    entrypoint: Literal["on_decision"] = "on_decision"
    api_contract: Literal["python-decision-api.v1"] = "python-decision-api.v1"
    state_model: Literal["stateless_per_decision"] = "stateless_per_decision"
    data_profile: Literal["m2.previous_close.v1"] = "m2.previous_close.v1"
    dependencies: tuple[Literal["decimal", "math"], ...] = ("decimal", "math")
    trust_model: Literal["owner_trusted_local"] = "owner_trusted_local"
    resource_profile: Literal["python-local-bounded.v1"] = "python-local-bounded.v1"

    @field_validator("dependencies")
    @classmethod
    def ordered(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Dependencies must be unique")
        return tuple(sorted(values))


class PythonDefinition(PythonDescriptor):
    template: Literal["trusted_python_decision.v1"] = "trusted_python_decision.v1"
    source_sha256: Sha
    sdk_sha256: Sha


class PythonCapabilities(Contract):
    api_contract: Literal["python-decision-api.v1"] = "python-decision-api.v1"
    registration: Literal["local_cli_only"] = "local_cli_only"
    trust_model: Literal["owner_trusted_local"] = "owner_trusted_local"
    state_model: Literal["stateless_per_decision"] = "stateless_per_decision"
    arbitrary_data_queries: Literal[False] = False
    hostile_code_sandbox: Literal[False] = False
    hard_memory_quota: Literal[False] = False
    module_load_seconds: int = 5
    callback_seconds: int = 5
    attempt_seconds: int = 120
    output_bytes: int = 1048576
    source_bytes: Literal[65536] = 65536
    parameters_bytes: Literal[8192] = 8192
    registration_status: Literal["registered_not_executed"] = "registered_not_executed"


def sdk_sha256():
    root = Path(__file__).parent
    paths = [
        root.parent / "strategy_api.py",
        root / "spec.py",
        root / "decision.py",
        root / "runtime.py",
    ]
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def parameters(value):
    if type(value) is not dict or len(value) > 32:
        raise ValueError("Use at most 32 flat strategy parameters")
    for key, item in value.items():
        if type(key) is not str or not key or len(key) > 80:
            raise ValueError("Parameter keys must be nonempty strings up to 80 characters")
        if item is not None and type(item) not in (str, int, bool):
            raise ValueError(
                "Values must be strings, strict integers, booleans or null; decimal amounts are strings"
            )
    if len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()) > PARAMETER_LIMIT:
        raise ValueError("Strategy parameters exceed 8 KiB")
    return value


def check_source(raw, descriptor):
    """Syntax/compatibility only; never import, compile-and-run or claim safety."""
    if not raw or len(raw) > SOURCE_LIMIT:
        raise DomainError("PYTHON_SOURCE_INVALID", "Use one nonempty UTF-8 file up to 64 KiB", 422)
    try:
        text = raw.decode("utf-8")
        tree = ast.parse(text, filename="strategy-source.py")
    except (UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
        raise DomainError(
            "PYTHON_SOURCE_INVALID", "Source must be valid UTF-8 Python syntax", 422
        ) from exc
    entries = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "on_decision"
    ]
    valid = len(entries) == 1 and type(entries[0]) is ast.FunctionDef
    if valid:
        args = entries[0].args
        valid = (
            not entries[0].decorator_list
            and not args.posonlyargs
            and not args.kwonlyargs
            and not args.vararg
            and not args.kwarg
            and not args.defaults
            and [a.arg for a in args.args] == ["context", "data", "parameters"]
        )
    if not valid:
        raise DomainError(
            "PYTHON_ENTRYPOINT_INVALID",
            "Define synchronous on_decision(context, data, parameters)",
            422,
        )
    allowed = set(descriptor.dependencies) | {"ashare_quant.strategy_api"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module] if not node.level else [None]
        else:
            continue
        if any(module not in allowed for module in modules):
            raise DomainError(
                "PYTHON_DEPENDENCY_UNSUPPORTED",
                "Only declared decimal/math and strategy_api imports are supported",
                422,
            )
    return text


def is_python(strategy):
    return isinstance(strategy.definition, PythonDefinition)


def task_source(root, strategy):
    from ashare_quant.m2_jobs.files import regular

    if not is_python(strategy):
        return None
    try:
        raw = regular(Path(root) / "strategy-source.py")
        if len(raw) > SOURCE_LIMIT or hashlib.sha256(raw).hexdigest() != strategy.source_sha256:
            raise ValueError
        return raw
    except (OSError, ValueError, DomainError) as exc:
        raise DomainError(
            "PYTHON_SOURCE_CHANGED",
            "Frozen task source is missing or differs; preserve the original task",
        ) from exc
