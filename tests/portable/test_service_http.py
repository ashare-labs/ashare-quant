"""Real loopback HTTP and independent CLI workers, including an API process restart."""

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .conftest import request


def test_loopback_api_restart_preserves_worker_queue_and_results(owner_store, tmp_path):
    owner, dataset = owner_store
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    database = tmp_path / "http.sqlite3"
    base = [sys.executable, "-m", "ashare_quant.cli", "--db", str(database)]
    env = os.environ | {
        "ASHARE_QUANT_M2_STORE": str(owner),
        "ASHARE_QUANT_JOB_POLICY": '{"max_workers":2,"max_pending":4}',
    }
    servers, workers = [], []
    with (tmp_path / "server.log").open("wb") as log:

        def start_server():
            process = subprocess.Popen(
                base + ["serve", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT
            )
            servers.append(process)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, "API startup failed; see server.log"
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{port}/api/v1/health", timeout=1, trust_env=False
                    )
                    if response.status_code == 200:
                        return process
                except httpx.ConnectError:
                    pass
                time.sleep(0.05)
            raise AssertionError("API readiness deadline exceeded")

        try:
            server = start_server()
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15
            ) as client:
                assert (
                    client.post("/api/v1/m2/data/bind", json={"dataset_id": dataset}).status_code
                    == 200
                )
                payloads = [request(client, window="m2a", key=f"http-{i}") for i in range(3)]

                def submit(index):
                    for _ in range(8):
                        response = client.post("/api/v1/m2/jobs", json=payloads[index])
                        if response.status_code == 503:
                            time.sleep(0.1)
                            continue
                        assert response.status_code == 201, response.text
                        return response.json()["job_id"]
                    raise AssertionError("HTTP admission deadline exceeded")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    ids = list(pool.map(submit, [0, 0, 1, 2]))
                assert ids[0] == ids[1] and len(set(ids)) == 3
                assert (
                    client.post(f"/api/v1/m2/jobs/{ids[-1]}/cancel", json={}).json()["status"]
                    == "cancelled"
                )
                for index in range(2):
                    output = (tmp_path / f"worker-{index}.log").open("wb")
                    process = subprocess.Popen(
                        base + ["job-worker", "--once"],
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                    output.close()
                    workers.append(process)
                # Abrupt HTTP facade death must not kill independent durable workers.
                server.kill()
                assert server.wait(timeout=5) != 0
                start_server()
                for process in workers:
                    assert process.wait(timeout=45) == 0
                for job_id in set(ids[:-1]):
                    response = client.get(f"/api/v1/m2/jobs/{job_id}")
                    assert response.status_code == 200, response.text
                    view = response.json()
                    assert view["status"] == "succeeded" and view["attempt_count"] == 1
                    result = client.get(f"/api/v1/m2/jobs/{job_id}/result")
                    assert result.status_code == 200, result.text
                    assert result.json()["manifest_sha256"] == view["manifest_sha256"]
                    assert result.headers["ETag"] == '"' + view["result"]["snapshot_sha256"] + '"'
                assert client.get(f"/api/v1/m2/jobs/{ids[-1]}").json()["status"] == "cancelled"
                evidence = {
                    "transport": "real_loopback_http",
                    "worker_processes": 2,
                    "abrupt_api_restarts": 1,
                    "submissions": 4,
                    "unique_jobs": 3,
                    "succeeded": 2,
                    "cancelled": 1,
                    "duplicate_accounts": 0,
                    "result_etags_verified": True,
                }
                (tmp_path / "http-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        finally:
            for process in servers + workers:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
