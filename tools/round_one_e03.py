"""Freeze and run E03 against an existing admitted owner product, entirely local."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.identity import source_sha256
from ashare_quant.m2_jobs.executor import Configuration
from ashare_quant.m2_jobs.service import Jobs
from ashare_quant.m2_jobs.worker import work_one
from ashare_quant.store import Store


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-store", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute", action="store_true", help="Execute the previously frozen preparation"
    )
    args = parser.parse_args()
    root = args.output.resolve()
    config = Configuration(root / "owner")
    if not args.execute:
        root.mkdir(parents=True, exist_ok=False)
        before = files(args.owner_store)
        shutil.copytree(args.owner_store, config.owner_store)
        prepared = root / "prepared"
        prepared.mkdir()
        app = create_app(str(prepared / "control.sqlite3"), m2_jobs_configuration=config)
        with TestClient(app) as client:
            binding = client.post("/api/v1/m2/data/bind", json={"dataset_id": args.dataset_id})
            assert binding.status_code == 200, binding.text
            catalogue = client.get("/api/v1/m2/jobs/catalogue").json()
            window = next(x for x in catalogue["windows"] if x["scope"]["window_id"] == "w1")
            request = dict(
                window_id="w1",
                name="E03 local durable acknowledgement boundary",
                idempotency_key="e03-same-session",
                assumption_ack_sha256=window["assumption_ack_sha256"],
                data_binding_sha256=catalogue["data_binding_sha256"],
                parameters=catalogue["parameters"]
                | {"enter_below": "100", "exit_above": "200", "target_shares": 100},
            )
            response = client.post("/api/v1/m2/jobs", json=request)
            assert response.status_code == 201, response.text
            job = response.json()
        for name in ("control", "interrupted"):
            shutil.copytree(prepared, root / name)
        assert before == files(args.owner_store)
        save(
            root / "input.json",
            dict(
                case_id="E03",
                source_sha256=source_sha256(),
                source_owner=str(args.owner_store.resolve()),
                source_owner_files=before,
                copied_owner_files=files(config.owner_store),
                dataset_id=args.dataset_id,
                request=request,
                job=job,
                prepared_files=files(prepared),
                fault="after_replace:2",
                boundary="First BAR native order/fill/account/decision/input-cursor atomically fsynced and replaced; process dies before persistence call returns",
                account_authority="RQAlpha single native account",
                data_scope="Existing conditional M2 product; E03 engineering replay only, not JQ market comparison",
                checks=[
                    "Same prepared session and input identity",
                    "First durable execution is a buy",
                    "Same decision/order/fill IDs",
                    "No first-buy submission in resumed attempt",
                    "Same final account and daily projections",
                    "Completed replay does not run native again",
                ],
            ),
        )
        print(json.dumps(dict(status="FROZEN_NOT_EXECUTED", input=str(root / "input.json"))))
        return
    frozen = json.loads((root / "input.json").read_text())
    assert frozen["source_sha256"] == source_sha256(), "E03 source changed after freeze"
    assert files(config.owner_store) == frozen["copied_owner_files"]
    job_id = frozen["job"]["job_id"]
    control = Jobs(Store(root / "control" / "control.sqlite3"), config)
    interrupted = Jobs(Store(root / "interrupted" / "control.sqlite3"), config)
    assert control.get(job_id).status == interrupted.get(job_id).status == "queued"
    work_one(control, job_id)
    assert control.get(job_id).status == "succeeded", control.get(job_id)
    save(root / "control-result.json", control.result(job_id).model_dump(mode="json"))
    work_one(interrupted, job_id, _fault=frozen["fault"])
    stopped = interrupted.get(job_id)
    save(root / "stopped.json", stopped.model_dump(mode="json"))
    assert stopped.status == "failed" and stopped.committed_cursor == 2 and stopped.can_resume, (
        stopped
    )
    directory = interrupted.directory(job_id)
    checkpoint_before_resume = sha(directory / "native.chk")
    interrupted.resume(job_id)
    work_one(interrupted, job_id)
    assert interrupted.get(job_id).status == "succeeded", interrupted.get(job_id)
    left, right = control.result(job_id), interrupted.result(job_id)
    save(root / "resumed-result.json", right.model_dump(mode="json"))
    assert left.summary == right.summary and left.account == right.account
    for field in ("signal", "account_at_day_close", "orders", "fills"):
        assert [getattr(d, field) for d in left.days] == [getattr(d, field) for d in right.days], (
            field
        )
    traces = {
        p.name: [json.loads(line) for line in p.read_text().splitlines()]
        for p in directory.glob("attempt-*.trace.jsonl")
    }
    # Examine trace order; never derive fills from the trace instead of native receipt.
    save(root / "attempt-traces.json", traces)
    final_checkpoint = sha(directory / "native.chk")
    assert work_one(interrupted, job_id) is None
    assert sha(directory / "native.chk") == final_checkpoint
    assert files(config.owner_store) == frozen["copied_owner_files"]
    save(
        root / "checks.json",
        dict(
            status="ACCOUNT_AND_ORDER_REPLAY_CHECKS_PASSED_ID_AUDIT_PENDING",
            case_id="E03",
            job_id=job_id,
            source_sha256=source_sha256(),
            fault=frozen["fault"],
            checkpoint_before_resume=checkpoint_before_resume,
            checkpoint_final=final_checkpoint,
            actual_process_interruption=True,
            worker_attempts=interrupted.get(job_id).attempt_count,
            same_summary=True,
            same_final_account=True,
            same_daily_signals_orders_fills=True,
            completed_reentry_no_native_run=True,
            owner_unchanged=True,
            new_jq_runs=0,
        ),
    )
    print(json.dumps(dict(status="EXECUTED_ID_AUDIT_PENDING", root=str(root))))


if __name__ == "__main__":
    main()
