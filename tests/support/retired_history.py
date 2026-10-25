"""Shared retired history fixtures; synthetic evidence only."""

from ashare_quant.storage.artifacts import atomic, canonical, digest, sha


def seed_history(app, family, *, status="succeeded"):
    """A constructed seal tests storage, without executing a retired algorithm."""
    schema = {"daily": "m2-job-manifest.v6", "portfolio": "portfolio-job-manifest.v1"}[family]
    job_id = "m2job-" + "a" * 32
    session = {"run_id": "a" * 32}
    request = {"name": "Preserved user research", "idempotency_key": "prior-run"}
    manifest = {"schema_version": schema, "job_id": job_id, "request": request,
                "session": session, "session_sha256": digest(session),
                "session_file_sha256": digest(session),
                "service_identity": {"source_sha256": "b" * 64}}
    root = app.state.history.directory(job_id)
    root.mkdir()
    atomic(root / "accepted-manifest.json", manifest)
    atomic(root / "session.json", session)
    value = {"job_id": job_id, "summary": {"final_equity": "100123.45"}, "source": "old-runtime"}
    evidence = {"manifest.json": canonical(manifest), "session.json": canonical(session),
                "result.json": canonical(value), "receipt.json": b"{}", "proof.json": b"{}",
                "native.chk": b"sealed-checkpoint"}
    sealed = root / "publication-1"
    sealed.mkdir()
    for name, raw in evidence.items():
        atomic(sealed / name, raw)
    seal = {"job_id": job_id, "manifest_sha256": digest(manifest),
            "files": {name: {"sha256": sha(raw), "size": len(raw)} for name, raw in evidence.items()}}
    atomic(sealed / "seal.json", seal)
    reference = dict(result_id=job_id, href="/old/endpoint", snapshot_sha256=digest(value),
                     evidence_manifest_sha256=digest(seal), publication="publication-1")
    with app.state.store.connection(transaction=True) as db:
        db.execute("INSERT INTO tasks(job_id,idempotency_key,request_hash,request,manifest,manifest_sha256,account_id,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, "prior-run", digest(request), canonical(request).decode(), canonical(manifest).decode(),
                    digest(manifest), "bt-" + "a" * 32, status, "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z",
                    canonical(reference).decode() if status == "succeeded" else None))
        db.execute("INSERT INTO registered_strategies VALUES (?, ?)",
                   ("c" * 64, canonical({"name": "User's retained strategy", "source": "user bytes"}).decode()))
    return job_id, root, value
