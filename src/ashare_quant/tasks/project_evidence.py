"""Check project snapshot evidence without importing any strategy modules."""

import unicodedata

from ashare_quant.storage.artifacts import digest, require, sha
from ashare_quant.tasks.evidence import evidence_hash


def verify_project(root, manifest):
    request = manifest["request"]
    project = request["project"]
    require(request["strategy_id"] == project["project_id"]
            and project["entrypoint"] in project["files"],
            "PYTHON_PROJECT_CHANGED", "Frozen project identity differs")
    source_root = root / "source"
    require(source_root.is_dir() and not source_root.is_symlink(),
            "PYTHON_PROJECT_CHANGED", "Frozen source directory is unavailable")
    for name in project["files"]:
        require((source_root / name).is_file(),
                "PYTHON_PROJECT_CHANGED", "Frozen project source differs")
