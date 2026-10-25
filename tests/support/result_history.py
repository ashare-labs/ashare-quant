"""Shared result history fixtures; synthetic evidence only."""

from ashare_quant.storage.artifacts import atomic, canonical, digest
from tests.analysis.fixtures import browser_fixture


def seed_python(app, result, *, request_fields=None, session_fields=None):
    job_id = result["job_id"]
    session = {"run_id": "synthetic-frozen-input"}
    session.update(session_fields or {})
    request = {"name": "frozen analysis fixture", "idempotency_key": "analysis-test"}
    request.update(request_fields or {})
    strategy = b"raise AssertionError('historical analysis must not execute this source')\n"
    from ashare_quant.storage.artifacts import sha

    manifest = {
        "schema_version": "python-demand-job.v2", "job_id": job_id, "request": request,
        "session": session, "session_sha256": digest(session), "session_file_sha256": digest(session),
        "service_identity": {"source_sha256": "b" * 64}, "files": {"strategy.py": sha(strategy)},
    }
    root = app.state.history.directory(job_id)
    root.mkdir()
    for name, value in (("accepted-manifest.json", manifest), ("session.json", session),
                        ("published.json", result), ("strategy.py", strategy)):
        atomic(root / name, value)
    reference = {"result_id": job_id, "href": "/retained-result", "snapshot_sha256": digest(result),
                 "evidence_manifest_sha256": digest(manifest), "publication": "publication-1"}
    with app.state.store.connection(transaction=True) as db:
        db.execute("INSERT INTO tasks(job_id,idempotency_key,request_hash,request,manifest,manifest_sha256,account_id,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, "analysis-test", digest(request), canonical(request).decode(), canonical(manifest).decode(),
                    digest(manifest), "bt-" + "a" * 32, "succeeded", "2025-01-01T00:00:00Z",
                    "2025-01-01T00:00:00Z", canonical(reference).decode()))
    return root

def frozen_fixture():
    result = browser_fixture()["result"]
    days = result["preparation"]["trading_dates"]
    result["native"]["benchmark"] = {
        "schema": "aq-benchmark-prices.v1", "security": "000300.XSHG", "status": "available",
        "price_basis": "published_close_price_return", "base": {"date": "2024-12-31", "close": 1000},
        "prices": [{"date": day, "close": 1000 + i + 1} for i, day in enumerate(days)],
        "receipt": {"receipt_sha256": "a" * 64},
    }
    return result
