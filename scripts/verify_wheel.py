"""Exercise installed execution paths in a new environment with no AQ source tree."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IDENTITY = """
import json
import ashare_quant
from ashare_quant.identity import source_sha256
from ashare_quant.providers.runtime import verify
from ashare_quant.runtime import runtime_fingerprint
verify()
runtime_fingerprint(native=True)
print(json.dumps({'version': ashare_quant.__version__, 'source_sha256': source_sha256(),
                  'module': ashare_quant.__file__}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--daily-components", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    wheel = args.wheel.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "PIP_", "PYTHON", "ASHARE_"))}
    env.update(UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1")
    if args.cache:
        env["UV_CACHE_DIR"] = str(args.cache.resolve())
    if args.offline:
        env["UV_OFFLINE"] = "1"
    if args.daily_components:
        env["ASHARE_QUANT_TEST_DAILY_COMPONENTS"] = str(args.daily_components.resolve())

    with tempfile.TemporaryDirectory(prefix="aq-wheel-") as temporary:
        root = Path(temporary)
        if not args.cache:
            env["UV_CACHE_DIR"] = str(root / "cache")
        python = root / "venv/bin/python"

        def run(name, command, cwd=project):
            with (output / (name + ".log")).open("w") as log:
                result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=log,
                                        stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"{name} failed: {output / (name + '.log')}")
            return (output / (name + ".log")).read_text()

        requirements = root / "requirements.txt"
        # Lint runs in the source check; the installed-runtime exercise needs no linter binary.
        run("export", ["uv", "export", "--locked", "--extra", "native", "--no-emit-project",
                       "--no-emit-package", "ruff", "--no-config", "--output-file", str(requirements)])
        run("venv", ["uv", "venv", "--no-config", "--python", sys.executable, str(root / "venv")])
        run("install-dependencies", ["uv", "pip", "install", "--no-config", "--python", str(python),
                                     "--require-hashes", "-r", str(requirements)])
        run("install-wheel", ["uv", "pip", "install", "--no-config", "--python", str(python),
                               "--no-deps", str(wheel)])
        installed = json.loads(run("installed-identity", [str(python), "-I", "-c", IDENTITY], root))
        assert Path(installed["module"]).is_relative_to(root / "venv"), installed
        source = json.loads(run("source-identity", [str(python), "-I", "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]);\n" + IDENTITY,
                            str(project / "src")], root))
        assert installed["source_sha256"] == source["source_sha256"]
        validation = root / "validation"
        validation.mkdir()
        for name in ("tests", "scripts", "examples"):
            shutil.copytree(project / name, validation / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(project / "pyproject.toml", validation / "pyproject.toml")
        tests = ["tests/test_api.py", "tests/test_contract_export.py", "tests/test_public_package.py",
                 "tests/minute/test_native.py", "tests/paper/test_native.py", "tests/paper/test_recovery.py",
                 "tests/portfolio/test_signals.py", "tests/execution/test_budget_account.py"]
        if args.daily_components:
            tests += ["tests/daily_tasks/test_review_binding.py",
                      "tests/daily_tasks/test_native.py::test_three_real_windows[two_day-2]",
                      "tests/daily_tasks/test_native.py::test_crash_and_resume_keeps_account[native_submit:2]",
                      "tests/daily_tasks/test_end_date_native.py::test_selected_prefix_matches_account_oracle_and_has_no_tail_events[w1-2020-01-03-hold-8735-9995-100-1-1]"]
        test_output = run("tests", [str(python), "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                   "--basetemp", str(root / "test-output"), *tests], validation)
        result = {"wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  "source_sha256": installed["source_sha256"], "noneditable_install": True,
                  "source_identity_matches": True, "validation_tree_contains_src": False,
                  "dependencies": "runtime and test dependencies with locked hashes, new environment",
                  "excluded_development_tools": ["ruff"], "offline": args.offline,
                  "daily_real_components": bool(args.daily_components),
                  "tests": tests, "test_summary": test_output.strip().splitlines()[-1]}
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
