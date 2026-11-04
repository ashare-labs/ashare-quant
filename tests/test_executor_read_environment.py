"""Isolated workers retain verified local I/O settings, never source overrides."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import ashare_data
from ashare_data.storage.verified_objects import verify_cached

from ashare_quant.tasks.executor import Executor

IO_SETTINGS = (
    "ASHARE_MARKET_VERIFY_CACHE", "ASHARE_DUCKDB_TEMP_DIRECTORY",
    "ASHARE_DUCKDB_MEMORY_LIMIT", "ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
    "ASHARE_MARKET_READ_REPLICAS",
)


def test_real_child_reuses_verified_object_and_configured_spill(tmp_path, monkeypatch):
    cache, spill, replicas = tmp_path / "verified", tmp_path / "spill", tmp_path / "replicas"
    spill.mkdir()
    replicas.mkdir(mode=0o700)
    path = tmp_path / "immutable-object"
    path.write_bytes(b"real immutable content, verified before child launch")
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    monkeypatch.setenv("ASHARE_DUCKDB_TEMP_DIRECTORY", str(spill))
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(replicas))
    for key in IO_SETTINGS[2:-1]:
        monkeypatch.delenv(key, raising=False)
    fingerprint = verify_cached(path, expected, path.stat().st_size,
                                lambda p: hashlib.sha256(p.read_bytes()).hexdigest())
    # A real isolated interpreter uses the same AD package selected by this test.
    # Failing on a hash callback proves it consumed the already checked receipt.
    code = '''
import json, os, sys
assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
sys.path.insert(0, sys.argv[1])
from ashare_data.storage.verified_objects import verify_cached
from ashare_data.storage.market_release import connection
def unexpected_hash(path):
    raise AssertionError("child discarded the validated-object cache")
observed = verify_cached(sys.argv[2], sys.argv[3], int(sys.argv[4]), unexpected_hash)
with connection() as db:
    spill = db.execute("SELECT current_setting('temp_directory')").fetchone()[0]
print(json.dumps({"fingerprint": observed, "spill": spill,
                  "replicas": os.environ.get("ASHARE_MARKET_READ_REPLICAS")}))
'''
    output = subprocess.check_output(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(Path(ashare_data.__file__).parents[1]),
         str(path), expected, str(path.stat().st_size)],
        env=Executor.environment(), text=True, timeout=30,
    )
    assert json.loads(output) == {"fingerprint": fingerprint, "spill": str(spill),
                                 "replicas": str(replicas)}


def test_only_explicit_io_settings_cross_the_worker_boundary(monkeypatch):
    for key in IO_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    assert not set(IO_SETTINGS) & Executor.environment().keys()
    configured = dict(zip(IO_SETTINGS, ("/private/cache", "/private/spill", "2GB", "8GB", "/private/replicas")))
    for key, value in configured.items():
        monkeypatch.setenv(key, value)
    forbidden = {
        "ASHARE_DEPLOYMENT", "ASHARE_CONSUMER_DEPLOYMENT", "ASHARE_DATA_ROOT",
        "ASHARE_QUANT_DAILY_STORE", "ASHARE_QUANT_FACTOR_SOURCES",
        "ASHARE_QUANT_MARKET_STORES", "AQ_API_URL", "QUANT_SERVICE_URL",
        "HTTP_PROXY", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
    }
    for key in forbidden:
        monkeypatch.setenv(key, "must-not-reach-child")
    env = Executor.environment()
    assert {key: env[key] for key in IO_SETTINGS} == configured
    assert not forbidden & env.keys()
