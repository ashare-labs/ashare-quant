"""Incremental task evidence; compression changes storage, never observation content."""

import gzip
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from ashare_quant.storage.artifacts import require, sync_dir


@contextmanager
def reading(path, *, text=False):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "M2_JOB_ARTIFACT_UNAVAILABLE")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt" if text else "rb") as stream:
        yield stream


def file_hash(path, *, decompressed=False):
    h = hashlib.sha256()
    require(Path(path).is_file() and not Path(path).is_symlink(), "M2_JOB_ARTIFACT_UNAVAILABLE")
    context = reading(path) if decompressed else open(path, "rb")
    with context as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value, *, before_publish=None):
    """Canonical JSON without materializing a second complete bytes/string copy."""
    path = Path(path)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    temporary = path.with_name("." + uuid.uuid4().hex + ".pending")
    h = hashlib.sha256()
    try:
        with temporary.open("xb") as stream:
            chunks, size = [], 0
            encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for text in encoder.iterencode(value):
                raw = text.encode()
                chunks.append(raw)
                size += len(raw)
                if size >= 65536:
                    block = b"".join(chunks)
                    stream.write(block)
                    h.update(block)
                    chunks, size = [], 0
            block = b"".join(chunks)
            stream.write(block)
            h.update(block)
            stream.flush()
            os.fsync(stream.fileno())
        if before_publish:
            before_publish()
        os.replace(temporary, path)
        sync_dir(path.parent)
        return h.hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def compress(path):
    """Seal a new task's completed file losslessly; never transform old tasks."""
    path = Path(path)
    target = path.with_suffix(path.suffix + ".gz")
    temporary = target.with_name("." + uuid.uuid4().hex + ".pending")
    expected = file_hash(path)
    try:
        with path.open("rb") as source, temporary.open("xb") as destination:
            with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0) as packed:
                while block := source.read(1024 * 1024):
                    packed.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
        require(file_hash(target, decompressed=True) == expected, "PYTHON_INPUT_CHANGED")
        sync_dir(path.parent)
        path.unlink()
        sync_dir(path.parent)
        return target
    finally:
        temporary.unlink(missing_ok=True)


class Journal:
    """A single gzip stream, constant writer state, no retained entry list."""

    def __init__(self, path):
        self.path, self.count, self.closed = Path(path), 0, False
        self.raw = self.path.open("xb")
        self.stream = gzip.GzipFile(filename="", fileobj=self.raw, mode="wb", mtime=0)

    def append(self, value):
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                   allow_nan=False, default=str)
        # One record can be large (an explicit history request), but is never copied
        # into a second full serialized string or retained after the call.
        chunks, size = [], 0
        for piece in encoder.iterencode(value):
            raw = piece.encode()
            chunks.append(raw)
            size += len(raw)
            if size >= 65536:
                self.stream.write(b"".join(chunks))
                chunks, size = [], 0
        self.stream.write(b"".join(chunks) + b"\n")
        self.count += 1

    def flush(self):
        if not self.closed:
            self.stream.flush()
            self.raw.flush()

    def __len__(self):
        return self.count

    def __iter__(self):
        self.flush()
        with reading(self.path, text=True) as stream:
            try:
                for line in stream:
                    yield json.loads(line)
            except EOFError:
                if self.closed:
                    raise
                # Diagnostic iteration of a still-open gzip member. Final seals
                # always close first and require a valid gzip trailer.

    def close(self):
        if not self.closed:
            self.stream.close()
            self.raw.flush()
            os.fsync(self.raw.fileno())
            self.raw.close()
            self.closed = True

    def seal(self):
        self.close()
        return {"file": self.path.name, "sha256": file_hash(self.path), "records": self.count,
                "encoding": "gzip-jsonl"}
