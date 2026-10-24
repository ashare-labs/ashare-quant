"""Check project snapshot evidence without importing any strategy modules."""

import unicodedata

from ashare_quant.storage.artifacts import digest, require, sha
from ashare_quant.tasks.evidence import evidence_hash


def verify_project(root, manifest):
    request = manifest["request"]
    project = request["project"]
    require(request["strategy_id"] == project["project_id"]
            and project["entrypoint"] in project["files"]
            and manifest["session"]["project_sha256"] == digest(project),
            "PYTHON_PROJECT_CHANGED", "Frozen project identity differs")
    expected = {"source/" + name: sha(source.encode()) for name, source in project["files"].items()}
    require({k: v for k, v in manifest["files"].items() if k.startswith("source/")} == expected,
            "PYTHON_PROJECT_CHANGED", "Frozen project source inventory differs")
    source_root = root / "source"
    require(source_root.is_dir() and not source_root.is_symlink(),
            "PYTHON_PROJECT_CHANGED", "Frozen source directory is unavailable")
    actual = set()
    for path in source_root.rglob("*"):
        require(not path.is_symlink(), "PYTHON_PROJECT_CHANGED", "Frozen project contains a link")
        if path.is_dir():
            continue
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        require(relative not in actual, "PYTHON_PROJECT_CHANGED", "Frozen project names collide")
        actual.add(relative)
    require(actual == expected.keys(), "PYTHON_PROJECT_CHANGED", "Frozen project file list differs")
    for name, hashed in expected.items():
        require(evidence_hash(root, name)[0] == hashed,
                "PYTHON_PROJECT_CHANGED", "Frozen project source differs")
