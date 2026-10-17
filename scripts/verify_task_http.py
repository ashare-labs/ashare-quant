"""One bounded local HTTP + independent worker smoke test. No external services."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main():
    with tempfile.TemporaryDirectory(prefix="ashare-quant-smoke-") as tmp:
        db = Path(tmp) / "control.sqlite3"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ | {"ASHARE_QUANT_DB": str(db)}
        with (Path(tmp) / "server.log").open("w+") as log:
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "ashare_quant.api:create_app",
                "--factory", "--host", "127.0.0.1", "--port", str(port)], env=env,
                stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
                    for _ in range(50):
                        if proc.poll() is not None:
                            raise RuntimeError("API exited before becoming ready")
                        try:
                            if client.get("/api/v1/health").status_code == 200:
                                break
                        except httpx.ConnectError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("API readiness timeout")
                    version = client.post("/api/v1/strategies", json={"name": "HTTP smoke", "scenario": "buy_hold"})
                    version.raise_for_status()
                    run = client.post("/api/v1/runs", json={
                        "strategy_version_id": version.json()["version_id"], "idempotency_key": "http-smoke"})
                    run.raise_for_status()
                    rid = run.json()["run_id"]
                    worker = subprocess.run([sys.executable, "-m", "ashare_quant.cli", "--db", str(db),
                        "worker", "--once"], capture_output=True, text=True, timeout=15)
                    assert worker.returncode == 0, worker.stdout + worker.stderr
                    state = client.get(f"/api/v1/runs/{rid}/state").json()
                    assert state["run"]["status"] == "succeeded"
                    assert state["account"]["cash"] == "9000.00"
                    assert len(state["fills"]) == 1
                    print(json.dumps({"result": "PASS", "scope": "local HTTP + separate worker synthetic replay",
                                      "is_test_double": True, "run_id": rid, "cash": "9000.00"}))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
