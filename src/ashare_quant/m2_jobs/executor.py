"""Run this installed, source-pinned executor in an isolated interpreter."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_sha256
from ashare_quant.python_strategies.supervision import BoundedOutput
from ashare_quant.runtime import runtime_fingerprint

from .files import lock, regular, require, sha
from .policy import ServicePolicy
from .process import stop
from .release import EXECUTION_SOURCE_SHA256, EXECUTION_VERSION


@dataclass(frozen=True)
class Configuration:
    owner_store: Path | None
    policy: ServicePolicy | None = None

    @classmethod
    def environment(cls):
        value = os.environ.get("ASHARE_QUANT_M2_STORE")
        return cls(Path(value).expanduser().resolve() if value else None, ServicePolicy.environment())

    def check(self):
        if self.owner_store is None:
            raise DomainError(
                "M2_JOB_NOT_CONFIGURED",
                "Configure the fixed A数达 M2 store on this local service",
                503,
            )
        if not self.owner_store.is_dir() or self.owner_store.is_symlink():
            raise DomainError(
                "M2_JOB_DATA_UNAVAILABLE", "Configured owner store is unavailable", 503
            )


def runner_sha():
    return sha(regular(Path(__file__).with_name("runner.py")))


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
        return Path(__file__).resolve().parents[2]

    def command(
        self,
        action,
        job_dir,
        window,
        invocation,
        session_file_sha=None,
        *,
        fault="none",
        control=None,
    ):
        self.config.check()
        package = self.package()
        site_root = str(metadata.distribution("rqalpha").locate_file(""))
        runner = Path(__file__).with_name("runner.py").resolve()
        payload = dict(
            action=action,
            root=str(job_dir),
            job_id=Path(job_dir).name,
            data_binding_sha256=sha(regular(Path(job_dir) / "data-binding.json")),
            execution_scope_sha256=sha(regular(Path(job_dir) / "execution-scope.json")),
            store=str(self.config.owner_store),
            window=window,
            invocation=invocation,
            session_file_sha256=session_file_sha,
            executor_source_sha256=EXECUTION_SOURCE_SHA256,
            executor_version=EXECUTION_VERSION,
            parameters=json.loads(regular(Path(job_dir) / "parameters.json")),
            parameters_sha256=sha(regular(Path(job_dir) / "parameters.json")),
            strategy_sha256=sha(regular(Path(job_dir) / "strategy.json")),
            runner_sha256=runner_sha(),
            fault=fault,
            control=control,
            resource_limits=self.policy.model_dump(mode="json"),
            parent_pid=os.getpid(),
        )
        bootstrap = (
            "import hashlib,json,runpy,sys; p,r,a=json.loads(sys.argv[1]); "
            "assert hashlib.sha256(open(r,'rb').read()).hexdigest()==a['runner_sha256']; "
            "sys.path[:0]=p; from ashare_quant.m2_jobs.process import child_limits; "
            "child_limits(a['resource_limits'],a['parent_pid'],a['action'], "
            "a['root']+'/'+a['invocation']+'.resource-limit.json'); "
            "runpy.run_path(r,init_globals={'TASK_ARGUMENTS':a},run_name='__main__')"
        )
        return [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            bootstrap,
            json.dumps([[str(package), site_root], str(runner), payload]),
        ]

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

    def immediate(self, action, job_dir, window, invocation, session_file_sha=None, *, lock_fds=()):
        command = self.command(action, job_dir, window, invocation, session_file_sha)
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
                            raise DomainError("M2_JOB_PREFLIGHT_TIMEOUT", "Fixed executor preparation timed out", 503)
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
                "Fixed executor refused owner data or frozen task identity",
                422,
                details={
                    "origin_code": error.get("code", "EXECUTOR_FAILED"),
                    "returncode": process.returncode,
                },
            )
        return json.loads(regular(job_dir / (invocation + ".proof.json")))
