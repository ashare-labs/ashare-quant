"""Launch the pinned paper worker; callers own leases, limits and recovery."""

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def spawn(request, log, serial, writer, fault=None):
    paths = [
        str(Path(__file__).resolve().parents[2]),
        str(metadata.distribution("ashare-data").locate_file("")),
        str(metadata.distribution("rqalpha").locate_file("")),
    ]
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['paper-native',a];from ashare_quant.paper.native import main;main()"
    env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "Asia/Shanghai",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "PAPER_PARENT_PID": str(os.getpid()),
    }
    if fault:
        if fault.startswith("checkpoint:"):
            env["PROBE_KILL"] = fault.removeprefix("checkpoint:")
        else:
            env["PAPER_FAULT"] = fault
    return subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, str(request)])],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        pass_fds=(serial.fileno(), writer.fileno()),
    )
