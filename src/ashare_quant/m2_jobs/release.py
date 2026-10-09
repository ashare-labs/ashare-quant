"""Identity of the separately sealed parameter executor, never the dev11 review claim."""

import hashlib
import json
from pathlib import Path

_raw = Path(__file__).with_name("executor-release.json").read_bytes()
RELEASE = json.loads(_raw)
RELEASE_SHA256 = hashlib.sha256(_raw).hexdigest()
EXECUTION_VERSION = RELEASE["version"]
EXECUTION_SOURCE_SHA256 = RELEASE["source_sha256"]
EXECUTION_WHEEL_SHA256 = RELEASE["wheel_sha256"]
