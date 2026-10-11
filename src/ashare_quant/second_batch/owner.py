"""Pinned public AD reads over a local process boundary; no copied market ledger."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


class OwnerError(RuntimeError):
    def __init__(self, response):
        self.code = response.get("code")
        super().__init__(response["message"])


class Owner:
    def __init__(self, binding, expected_sha256, paths, audit, *, expected_sources):
        code_root = Path(paths[0])
        observed_sources = {
            str(p.relative_to(code_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(code_root.rglob("*.py"))
        }
        if observed_sources != expected_sources:
            raise RuntimeError("SECOND_OWNER_CODE_HASH")
        raw = Path(binding).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("SECOND_OWNER_BINDING_HASH")
        worker = str(Path(__file__).with_name("owner_worker.py"))
        boot = "import json,sys,runpy;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=a;runpy.run_path(a[0],run_name='__main__')"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                boot,
                json.dumps([paths, [worker, str(binding)]]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.audit = Path(audit)
        self.binding = json.loads(raw)

    def call(self, operation, **parameters):
        request = dict(operation=operation, parameters=parameters)
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("SECOND_OWNER_PROCESS_FAILED: " + self.proc.stderr.read())
        response = json.loads(line)
        with self.audit.open("a") as f:
            f.write(json.dumps(dict(request=request, response=response), sort_keys=True) + "\n")
        if not response["ok"]:
            raise OwnerError(response)
        return response["value"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        if self.proc.returncode:
            raise RuntimeError(self.proc.stderr.read())
