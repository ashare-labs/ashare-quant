"""The runtime snapshots derive from the lock and must never drift silently."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def check(root, native=False):
    return subprocess.run(
        [sys.executable, str(root / "scripts/freeze_runtime.py"), "--check",
         *(["--native"] if native else [])],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize("native", [False, True])
def test_current_snapshots_match_the_locked_installation(native):
    result = check(ROOT, native)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("filename,native", [
    ("runtime-lock.json", False), ("native-runtime-lock.json", True), ("lock.sha256", False),
])
def test_stale_snapshot_check_fails_without_rewriting(tmp_path, filename, native):
    names = ["pyproject.toml", "uv.lock", "scripts/freeze_runtime.py",
             "src/ashare_quant/runtime-lock.json", "src/ashare_quant/native-runtime-lock.json",
             "src/ashare_quant/lock.sha256"]
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    changed = tmp_path / "src/ashare_quant" / filename
    changed.write_text("stale test snapshot\n")
    before = {name: (tmp_path / name).read_bytes() for name in names}
    result = check(tmp_path, native)
    assert result.returncode != 0 and filename in result.stderr
    assert {name: (tmp_path / name).read_bytes() for name in names} == before
