"""Run a sealed parameter executor wheel in an isolated interpreter, never the service package."""

import io
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from ashare_quant.contracts import DomainError

from .files import atomic, lock, regular, require, sha, sync_dir
from .release import EXECUTION_SOURCE_SHA256, EXECUTION_VERSION, EXECUTION_WHEEL_SHA256


@dataclass(frozen=True)
class Configuration:
    owner_store: Path | None

    @classmethod
    def environment(cls):
        value = os.environ.get("ASHARE_QUANT_M2_STORE")
        return cls(Path(value).expanduser().resolve() if value else None)

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


def wheel_bytes():
    raw = regular(Path(__file__).with_name("parameter-executor.whl.evidence"))
    require(
        sha(raw) == EXECUTION_WHEEL_SHA256,
        "M2_JOB_EXECUTOR_CHANGED",
        "Sealed parameter executor wheel differs",
    )
    return raw


def runner_sha():
    return sha(regular(Path(__file__).with_name("runner.py")))


class Executor:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config

    def package(self):
        raw = wheel_bytes()
        target = self.root / "executor-parameters-v1"
        with lock(self.root / "executor.lock", blocking=True):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                expected = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
                require(
                    all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in expected)
                )
                if not target.exists():
                    temp = self.root / ("executor-" + uuid.uuid4().hex + ".pending")
                    temp.mkdir(mode=0o700)
                    try:
                        for name, content in expected.items():
                            p = temp / name
                            p.parent.mkdir(parents=True, exist_ok=True)
                            atomic(p, content)
                        os.replace(temp, target)
                        sync_dir(self.root)
                    finally:
                        if temp.exists():
                            shutil.rmtree(temp)
                require(not target.is_symlink(), "M2_JOB_EXECUTOR_CHANGED")
                paths = list(target.rglob("*"))
                require(not any(p.is_symlink() for p in paths), "M2_JOB_EXECUTOR_CHANGED")
                require(
                    {p.relative_to(target).as_posix() for p in paths if p.is_file()}
                    == set(expected),
                    "M2_JOB_EXECUTOR_CHANGED",
                )
                for name, content in expected.items():
                    require(regular(target / name) == content, "M2_JOB_EXECUTOR_CHANGED")
        return target

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
            store=str(self.config.owner_store),
            window=window,
            invocation=invocation,
            session_file_sha256=session_file_sha,
            executor_source_sha256=EXECUTION_SOURCE_SHA256,
            executor_version=EXECUTION_VERSION,
            parameters=json.loads(regular(Path(job_dir) / "parameters.json")),
            parameters_sha256=sha(regular(Path(job_dir) / "parameters.json")),
            runner_sha256=runner_sha(),
            fault=fault,
            control=control,
        )
        bootstrap = (
            "import hashlib,json,runpy,sys; p,r,a=json.loads(sys.argv[1]); "
            "assert hashlib.sha256(open(r,'rb').read()).hexdigest()==a['runner_sha256']; "
            "sys.path[:0]=p; runpy.run_path(r,init_globals={'TASK_ARGUMENTS':a},run_name='__main__')"
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
        with (job_dir / (invocation + ".runner.log")).open("wb") as log:
            try:
                process = subprocess.run(
                    command,
                    env=self.environment(),
                    cwd=job_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                    check=False,
                    pass_fds=lock_fds,
                )
            except subprocess.TimeoutExpired:
                raise DomainError(
                    "M2_JOB_PREFLIGHT_TIMEOUT", "Fixed executor preparation timed out", 503
                ) from None
        if process.returncode:
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
