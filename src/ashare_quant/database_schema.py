"""Current control schema. Used only to initialize a disposable new database.

Existing databases are validated against this schema and never receive startup DDL.
Keeping the original additive column layout preserves current v3 control databases.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    version_id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m2_strategies (
    version_id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m2_python_sources (
    version_id TEXT PRIMARY KEY REFERENCES m2_strategies(version_id), source BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL REFERENCES strategies(version_id),
    account_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('queued','running','succeeded','failed','cancelled')),
    manifest TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    checkpoint TEXT,
    lease_token TEXT,
    lease_until REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_engine_per_account
    ON runs(account_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    observed_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS engine_fill_once
    ON events(run_id, json_extract(body, '$.fill.fill_id'))
    WHERE json_extract(body, '$.fill.fill_id') IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS engine_action_once
    ON events(run_id, json_extract(body, '$.action_id'))
    WHERE json_extract(body, '$.action_id') IS NOT NULL;
CREATE TABLE IF NOT EXISTS native_receipts (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), body TEXT NOT NULL
);
ALTER TABLE runs ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE runs ADD COLUMN reconciliation_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN native_started INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN native_reconcile_only INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS m2_jobs (
    job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL, request TEXT NOT NULL,
    manifest TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
    account_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN
        ('queued','running','cancelling','cancelled','failed','succeeded')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT, lease_until REAL,
    cancellation_requested_at TEXT,
    committed_cursor INTEGER NOT NULL DEFAULT 0,
    checkpoint_complete INTEGER NOT NULL DEFAULT 0,
    checkpoint_sha256 TEXT,
    result TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS m2_job_logs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES m2_jobs(job_id),
    at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS m2_jobs_queue ON m2_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS m2_logs_job ON m2_job_logs(job_id,sequence);
CREATE TABLE IF NOT EXISTS m2_job_service (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    policy TEXT NOT NULL, policy_sha256 TEXT NOT NULL
);
PRAGMA user_version=3;
"""
