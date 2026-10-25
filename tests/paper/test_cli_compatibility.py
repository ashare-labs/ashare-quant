"""Existing module invocations keep their parsers and refusal wire format."""

import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "online_cli", "intraday_cli", "continuation_cli", "successor_cli",
    "runtime_migration_cli", "settled_migration_cli",
])
def test_operator_module_help_remains_available(module):
    result = subprocess.run([sys.executable, "-B", "-m", "ashare_quant.paper." + module,
                             "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--root" in result.stdout


def test_inspection_keeps_root_before_command(tmp_path):
    root = tmp_path / "missing"
    result = subprocess.run([sys.executable, "-B", "-m", "ashare_quant.paper",
                             "--root", str(root), "sessions", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not root.exists()


@pytest.mark.parametrize("module,binding", [
    ("runtime_migration_cli", True), ("settled_migration_cli", False),
])
def test_migration_refusal_preserves_json_exit_code_and_no_account_creation(tmp_path, module, binding):
    root = tmp_path / "未创建账户"
    args = [sys.executable, "-B", "-m", "ashare_quant.paper." + module,
            "--root", str(root), "--state-sha", "a" * 64,
            "--from-sha", "b" * 64, "--to-sha", "c" * 64]
    if binding:
        args += ["--binding-sha", "d" * 64]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "blocked", "error": {
            "code": "PAPER_EXISTING_ACCOUNT_REQUIRED",
            "message": "PAPER_EXISTING_ACCOUNT_REQUIRED",
        },
    }
    assert not root.exists()
