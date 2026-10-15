"""Real loopback HTTP/CLI and standalone equivalence against caller-authorized AD exports.

Runs only in a NEW output directory. No data acquisition, original store writes or services retained.
Use the same script with an installed wheel to verify package behavior.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import ashare_quant
from ashare_quant.identity import source_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", required=True, type=Path)
    parser.add_argument("--long-export", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / "control.sqlite3"
    prefix = [sys.executable, "-B", "-m", "ashare_quant.cli", "--db", str(database)]
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }

    def save(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def command(arguments, label):
        run = subprocess.run(prefix + arguments, env=env, capture_output=True, text=True)
        (root / (label + ".stdout")).write_text(run.stdout)
        (root / (label + ".stderr")).write_text(run.stderr)
        assert run.returncode == 0, (label, run.returncode, run.stdout, run.stderr)
        return json.loads(run.stdout)

    inputs = {}
    for window in ("w2020", "w2022", "w2024"):
        path = args.exports / (window + "-model.json")
        inputs[window] = command(
            ["portfolio-data-import", "--input", str(path), "--name", window], "import-" + window
        )
    inputs["long"] = command(
        ["portfolio-data-import", "--input", str(args.long_export), "--name", "long"], "import-long"
    )
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    base = f"http://127.0.0.1:{port}/api/v1/portfolio"

    def http(path, body=None, expected=200):
        request = Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with opener.open(request, timeout=90) as response:
                status, raw = response.status, response.read()
        except HTTPError as error:
            status, raw = error.code, error.read()
        value = json.loads(raw)
        assert status == expected, (path, status, value)
        return value

    evidence = {
        "schema": "portfolio-api-acceptance.v1",
        "backend_version": ashare_quant.__version__,
        "module_path": ashare_quant.__file__,
        "source_sha256": source_sha256(),
        "cases": [],
    }
    with (root / "server.log").open("wb") as log:
        server = subprocess.Popen(
            prefix + ["serve", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    catalogue = http("/catalogue")
                    break
                except (URLError, ConnectionError):
                    assert server.poll() is None and time.monotonic() < deadline, (
                        "Server did not become ready"
                    )
                    time.sleep(0.1)
            assert catalogue["native_ready"]
            save(root / "catalogue.json", catalogue)
            save(root / "openapi.json", http("/openapi.json"))
            cases = [
                (
                    "w2020",
                    ["000001.XSHE", "600036.XSHG"],
                    "2020-02-03",
                    "2020-02-14",
                    "equal-weight",
                ),
                (
                    "w2022",
                    ["000333.XSHE", "000651.XSHE", "601318.XSHG"],
                    "2022-04-01",
                    "2022-04-15",
                    "equal-weight",
                ),
                (
                    "w2024",
                    ["000001.XSHE", "600036.XSHG", "600519.XSHG", "000858.XSHE"],
                    "2024-01-03",
                    "2024-01-12",
                    "momentum",
                ),
                (
                    "long",
                    ["000001.XSHE", "000651.XSHE", "600036.XSHG"],
                    inputs["long"]["trading_dates"][0],
                    inputs["long"]["trading_dates"][-1],
                    "momentum-low-volatility",
                ),
            ]
            for window, ids, start, end, algorithm in cases:
                case_root = root / window
                case_root.mkdir()
                selection = dict(
                    dataset_id=inputs[window]["dataset_id"],
                    securities=ids,
                    start_date=start,
                    end_date=end,
                    accept_retrospective_state=True,
                )
                if window == "long":
                    blocked = http("/preflight", selection)
                    assert not blocked["ready"] and blocked["gaps"]
                    save(case_root / "covered-refusal.json", blocked)
                    selection["mode"] = "price_only_no_limits"
                preflight = http("/preflight", selection)
                assert preflight["ready"], preflight
                save(case_root / "preflight.json", preflight)
                strategy = http("/strategies", {"algorithm": algorithm}, 201)
                request = selection | dict(
                    strategy_version_id=strategy["version_id"],
                    name="HTTP acceptance " + window,
                    idempotency_key="http-" + window,
                    initial_cash="1000000.00",
                    assumption_ack_sha256=preflight["assumption_ack_sha256"],
                )
                save(case_root / "request.json", request)
                job = http("/jobs", request, 201)
                assert http("/jobs", request, 201) == job
                save(case_root / "accepted.json", job)
                command(["job-worker", "--job-id", job["job_id"]], "worker-" + window)
                view = http("/jobs/" + job["job_id"])
                assert view["status"] == "succeeded", view
                result = http("/jobs/" + job["job_id"] + "/result")
                assert result["scope"]["timezone"] == "Asia/Shanghai"
                assert all(
                    datetime.fromisoformat(t["at"]).tzinfo is not None
                    for t in result["days"] + result["orders"] + result["trades"]
                )
                assert sum(Decimal(day["day_fee"]) for day in result["days"]) == Decimal(
                    result["summary"]["fee_total"]
                )
                assert result["days"][-1]["cumulative_fee"] == result["summary"]["fee_total"]
                save(case_root / "result.json", result)
                save(case_root / "logs.json", http("/jobs/" + job["job_id"] + "/logs"))
                manifest = http("/jobs/" + job["job_id"] + "/manifest")
                save(case_root / "manifest.json", manifest)
                assert http("/jobs", request, 201) == view
                second = command(["job-worker", "--job-id", job["job_id"]], "no-repeat-" + window)
                assert second["processed_job_id"] is None
                frozen = Path(str(database) + ".m2-jobs") / job["job_id"]
                direct = [
                    sys.executable,
                    "-B",
                    "-m",
                    "ashare_quant.daily_portfolio",
                    "--manifest",
                    str(frozen / "execution.json"),
                    "--input",
                    str(frozen / "input.json"),
                    "--strategy",
                    algorithm,
                    "--window",
                    "selected",
                    "--output",
                    str(case_root / "standalone"),
                    "--accept-owner-state-model",
                ]
                if window == "long":
                    direct += ["--accept-price-only-research"]
                run = subprocess.run(direct, env=env, capture_output=True, text=True)
                (case_root / "standalone.stdout").write_text(run.stdout)
                (case_root / "standalone.stderr").write_text(run.stderr)
                assert run.returncode == 0, run.stderr
                assert (
                    json.loads((case_root / "standalone/result.json").read_text())
                    == result["receipt"]
                )
                assert result["audit"]["passed"] and result["trades"]
                evidence["cases"].append(
                    dict(
                        window=window,
                        job_id=job["job_id"],
                        account_id=result["account_id"],
                        scope=result["scope"],
                        summary=result["summary"],
                        audit=result["audit"],
                        result_sha256=hashlib.sha256(
                            (case_root / "result.json").read_bytes()
                        ).hexdigest(),
                        duplicate_post_preserved=True,
                        completed_worker_reentry_no_execution=True,
                        standalone_receipt_equal=True,
                    )
                )
                print(
                    json.dumps(
                        {
                            "case": window,
                            "trades": len(result["trades"]),
                            "days": len(result["days"]),
                            "economic_scope": result["economic_scope"],
                        }
                    ),
                    flush=True,
                )
            assert len({x["account_id"] for x in evidence["cases"]}) == len(cases)
            assert len(http("/jobs")["items"]) == len(cases)
            save(root / "service.json", http("/service"))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    # Reopen through a fresh CLI process; successful read does not start a worker.
    for case in evidence["cases"]:
        result = command(["portfolio-result", case["job_id"]], "reopen-" + case["window"])
        assert result == json.loads((root / case["window"] / "result.json").read_text())
    evidence["fresh_process_results_equal"] = True
    evidence["server_stopped"] = server.poll() is not None
    save(root / "evidence.json", evidence)
    print(
        json.dumps(
            {
                "output": str(root),
                "source_sha256": evidence["source_sha256"],
                "cases": len(evidence["cases"]),
            }
        )
    )


if __name__ == "__main__":
    main()
