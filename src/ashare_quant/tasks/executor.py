"""Run this installed, source-pinned executor in an isolated interpreter."""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.identity import package_root, source_sha256
from ashare_quant.runtime import runtime_fingerprint
from ashare_quant.storage.artifacts import lock, regular, require
from ashare_quant.strategies.python.supervision import BoundedOutput
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.process import stop
from ashare_quant.tasks.release import EXECUTION_SOURCE_SHA256


@dataclass(frozen=True)
class Configuration:
    owner_store: Path | None
    policy: ServicePolicy | None = None

    @classmethod
    def environment(cls):
        value = os.environ.get("ASHARE_QUANT_DAILY_STORE")
        return cls(Path(value).expanduser().resolve() if value else None, ServicePolicy.environment())





class Executor:
    def __init__(self, root, config, policy=None):
        self.root, self.config = Path(root), config
        self.policy = policy or config.policy or ServicePolicy()

    def package(self):
        require(
            source_sha256() == EXECUTION_SOURCE_SHA256,
            "M2_JOB_EXECUTOR_CHANGED",
            "Installed executor source differs",
        )
        runtime_fingerprint(native=True)
        return package_root()


    @staticmethod
    def environment():
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
        env.update(
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
        )
        return env

    def immediate(self, action, job_dir, invocation, session_file_sha=None, *, lock_fds=()):
        command = self.command(action, job_dir, invocation, session_file_sha)
        with lock(self.root / "preparation.lock", timeout=self.policy.admission_wait_seconds) as preparation:
            with (job_dir / (invocation + ".runner.log")).open("wb") as log:
                process = subprocess.Popen(command, env=self.environment(), cwd=job_dir,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           pass_fds=(*lock_fds, preparation.fileno()), start_new_session=True)
                capture = BoundedOutput(process.stdout, log, self.policy.output_bytes)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if capture.exceeded.is_set():
                            raise DomainError("M2_JOB_OUTPUT_LIMIT", "Preparation output limit exceeded", 503)
                        if time.monotonic() - started > self.policy.preparation_seconds:
                            raise DomainError("M2_JOB_PREFLIGHT_TIMEOUT", "Executor preparation timed out", 503)
                        time.sleep(0.03)
                finally:
                    stop(process)
                    try:
                        capture.finish()
                    finally:
                        process.stdout.close()
                if capture.exceeded.is_set():
                    raise DomainError("M2_JOB_OUTPUT_LIMIT", "Preparation output limit exceeded", 503)
        if process.returncode:
            error_file = job_dir / (invocation + ".resource-limit.json")
            if not error_file.exists():
                error_file = job_dir / (invocation + ".runner-error.json")
            error = json.loads(regular(error_file)) if error_file.exists() else {}
            raise DomainError(
                "M2_JOB_PREFLIGHT_REFUSED",
                "Executor refused owner data or frozen task identity",
                422,
                details={
                    "origin_code": error.get("code", "EXECUTOR_FAILED"),
                    "returncode": process.returncode,
                },
            )
        return json.loads(regular(job_dir / (invocation + ".proof.json")))
