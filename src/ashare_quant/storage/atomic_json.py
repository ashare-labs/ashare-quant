"""Atomic, deterministic JSON file publication."""
import json
import os


def atomic(path, value):
    tmp = path.with_suffix(path.suffix + ".pending")
    with tmp.open("w") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
