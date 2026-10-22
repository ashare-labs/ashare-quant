"""Adapter to the existing isolated process supervisor and Python minute runner."""

import json
import os
import sys
from importlib import metadata
from pathlib import Path

from ashare_quant.identity import source_sha256
from ashare_quant.storage.artifacts import regular, sha
from ashare_quant.tasks.daily.executor import Executor as TaskExecutor


class Executor(TaskExecutor):
    def runner(self):
        return Path(__file__).with_name("runner.py")

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
        package = self.package()
        runner = self.runner()
        args = dict(
            action=action,
            root=str(job_dir),
            invocation=invocation,
            fault=fault,
            session_file_sha256=session_file_sha,
            control=control,
            resource_limits=self.policy.model_dump(mode="json"),
            parent_pid=os.getpid(),
            runner_sha256=sha(regular(runner)),
            source_sha256=source_sha256(),
        )
        paths = [
            str(package),
            str(metadata.distribution("rqalpha").locate_file("")),
            str(metadata.distribution("ashare-data").locate_file("")),
        ]
        boot = (
            "import hashlib,json,runpy,sys;p,r,a=json.loads(sys.argv[1]);"
            "assert hashlib.sha256(open(r,'rb').read()).hexdigest()==a['runner_sha256'];"
            "sys.path[:0]=p;from ashare_quant.tasks.process import child_limits;"
            "child_limits(a['resource_limits'],a['parent_pid'],a['action'],"
            "a['root']+'/'+a['invocation']+'.resource-limit.json');"
            "runpy.run_path(r,init_globals={'TASK_ARGUMENTS':a},run_name='__main__')"
        )
        return [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            boot,
            json.dumps([paths, str(runner), args]),
        ]
