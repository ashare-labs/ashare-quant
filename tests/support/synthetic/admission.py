"""The same accepted identity is checked by supervisor and native child."""

import hashlib
import json
from importlib import metadata
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path

from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_inventory, source_sha256
from ashare_quant.runtime import runtime_fingerprint
from ashare_quant.storage.store import digest
from tests.support.synthetic.engine.specs import RQ_IDENTITY, rq_specifications
from tests.support.synthetic.replay import specifications


def native_runtime_identity():
    # Verify bytes without importing the engine in the API/supervisor process.
    try:
        root = Path(find_spec("rqalpha").origin).parent
        expected = json.loads(files("ashare_quant.execution.rqalpha").joinpath("upstream-sha256.json").read_text())
        if any(hashlib.sha256((root / name).read_bytes()).hexdigest() != sha for name, sha in expected.items()):
            raise RuntimeError("UNSUPPORTED_RQALPHA_CONTENT")
        return {"upstream": expected, "dependencies": {name: metadata.version(name) for name in
            ("rqalpha", "numpy", "pandas", "jsonpickle", "simplejson", "logbook")}}
    except (RuntimeError, OSError, ValueError, ImportError, AttributeError) as exc:
        raise DomainError("ENGINE_CHANGED", "Native upstream content is not admitted") from exc


def validate_manifest(manifest):
    if manifest.backend_version != __version__ or manifest.backend_source_sha256 != source_sha256():
        raise DomainError("BACKEND_CHANGED", "Resume with the accepted backend and adapter sources")
    if manifest.implementation_files != source_inventory():
        raise DomainError("BACKEND_CHANGED", "Installed implementation inventory differs from acceptance")
    if manifest.dependency_lock_sha256 != files("ashare_quant").joinpath("lock.sha256").read_text().strip():
        raise DomainError("DEPENDENCIES_CHANGED", "Resume with the pinned dependency lock")
    if not manifest.runtime_versions:
        raise DomainError("RUNTIME_FINGERPRINT_MISSING", "Legacy run has no verified runtime inventory")
    if manifest.runtime_versions != runtime_fingerprint():
        raise DomainError("RUNTIME_DEPENDENCY_MISMATCH", "Installed dependencies differ from accepted run")
    native = manifest.engine.engine_id == "rqalpha"
    scenario = manifest.strategy.definition.scenario
    if digest((rq_specifications() if native else specifications())[scenario]) != manifest.fixture_spec_sha256:
        raise DomainError("SPEC_CHANGED", "Resume with the pinned scenario specification")
    if native and (manifest.engine != RQ_IDENTITY or manifest.native_runtime != native_runtime_identity()):
        raise DomainError("ENGINE_CHANGED", "Native implementation differs from accepted engine")
