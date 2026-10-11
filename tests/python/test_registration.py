"""Ordinary trusted-local interface tests; registration never evaluates source."""

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.m2.parameter_policy import RegisteredPythonParameters
from ashare_quant.m2_jobs.service import Jobs
from ashare_quant.python_strategies.spec import PythonDescriptor
from ashare_quant.python_strategies.supervision import BoundedOutput, check_phase

NORMAL = """from ashare_quant.strategy_api import TargetShares
print("OWNED_MODULE_LOADED")
def on_decision(context, data, parameters):
    return TargetShares(100)
"""


def test_local_registration_is_immutable_and_http_is_metadata_only(store, tmp_path, capsys):
    file = tmp_path / "strategy.py"
    file.write_text(NORMAL)
    jobs = Jobs(store)
    version = jobs.register_python(file, PythonDescriptor())
    assert capsys.readouterr().out == ""
    other = tmp_path / "renamed.py"
    other.write_text(NORMAL)
    assert jobs.register_python(other, PythonDescriptor()).version_id == version.version_id
    other.write_text(NORMAL + "\n# new version\n")
    assert jobs.register_python(other, PythonDescriptor()).version_id != version.version_id
    with TestClient(create_app(str(store.path))) as client:
        url = "/api/v1/m2/python-strategies"
        assert len(client.get(url).json()) == 2
        assert client.get(url + "/" + version.version_id).json()["version_id"] == version.version_id
        assert client.post(url, json={"source": NORMAL}).status_code == 405
        c = client.get("/api/v1/m2/jobs/catalogue").json()
        assert c["contract_version"] == "m2-jobs.v6"
        assert c["python_strategies"]["registration_status"] == "registered_not_executed"
        assert c["python_strategies"]["hostile_code_sandbox"] is False
        assert client.get("/api/v1/m2/jobs").json() == {"items": []}
    assert "OWNED_MODULE_LOADED" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "source,code",
    [
        ("bad python (", "PYTHON_SOURCE_INVALID"),
        ("def on_decision(context):\n    return 0\n", "PYTHON_ENTRYPOINT_INVALID"),
        ("import fractions\n" + NORMAL, "PYTHON_DEPENDENCY_UNSUPPORTED"),
        (
            "async def on_decision(context, data, parameters):\n    return None\n",
            "PYTHON_ENTRYPOINT_INVALID",
        ),
    ],
)
def test_normal_registration_errors(store, tmp_path, source, code):
    file = tmp_path / "invalid.py"
    file.write_text(source)
    with pytest.raises(DomainError) as e:
        Jobs(store).register_python(file, PythonDescriptor())
    assert e.value.code == code
    assert Jobs(store).python_strategies() == ()


@pytest.mark.parametrize("parameters", [{"amount": 1.5}, {"nested": {}}, {"long": "x" * 8192}])
def test_flat_parameters_reject_unsupported_values(parameters):
    with pytest.raises(ValidationError):
        RegisteredPythonParameters(strategy_version_id="a" * 64, strategy_parameters=parameters)


def test_cli_registration_does_not_load_module(tmp_path):
    file = tmp_path / "own.py"
    file.write_text(NORMAL)
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}")
    base = [sys.executable, "-m", "ashare_quant.cli", "--db", str(tmp_path / "control.sqlite3")]
    result = subprocess.run(
        base
        + ["job-strategy-register-python", "--file", str(file), "--descriptor", str(descriptor)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    version = json.loads(result.stdout)["version_id"]
    assert "OWNED_MODULE_LOADED" not in result.stdout + result.stderr
    assert (
        subprocess.run(base + ["job-python-strategy-get", version], capture_output=True).returncode
        == 0
    )


def test_ordinary_output_and_deadline_bounds(tmp_path):
    read, write = os.pipe()
    with os.fdopen(read, "rb") as incoming, (tmp_path / "log").open("wb") as log:
        capture = BoundedOutput(incoming, log, limit=10)
        os.write(write, b"normal log text")
        os.close(write)
        capture.finish()
        assert capture.exceeded.is_set()
    assert (tmp_path / "log").read_bytes() == b"normal log"
    phase = tmp_path / "phase.json"
    phase.write_text(
        json.dumps(
            dict(
                phase="CALLBACK",
                started_monotonic=time.monotonic() - 6,
                strategy_version_id="a" * 64,
                logical_at="2020-01-03T09:00:00+08:00",
                input_sha256="b" * 64,
            )
        )
    )
    with pytest.raises(DomainError) as e:
        check_phase(phase)
    assert e.value.code == "PYTHON_PHASE_TIMEOUT"
