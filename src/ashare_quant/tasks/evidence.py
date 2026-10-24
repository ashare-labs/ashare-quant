"""Hash-verified history streams; file size is not a second publication gate."""

import hashlib
import json
import mmap
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import PurePosixPath

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import require
from ashare_quant.tasks.storage_budget import require_free_space

CHUNK_BYTES = 65536


@contextmanager
def evidence_stream(root, name):
    require(isinstance(name, str), "HISTORY_EVIDENCE_PATH", "Invalid evidence path")
    parts = PurePosixPath(name)
    require(not parts.is_absolute() and parts.parts and "\\" not in name
            and all(p not in {".", ".."} for p in parts.parts), "HISTORY_EVIDENCE_PATH")
    current = root
    require(not current.is_symlink(), "HISTORY_EVIDENCE_PATH")
    for part in parts.parts:
        current = current / part
        require(not current.is_symlink(), "HISTORY_EVIDENCE_PATH")
    with os.fdopen(os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "HISTORY_EVIDENCE_PATH")
        yield stream


def evidence_file(root, name):
    with evidence_stream(root, name) as stream:
        return stream.read()


def evidence_hash(root, name):
    hashed, size = hashlib.sha256(), 0
    with evidence_stream(root, name) as stream:
        while block := stream.read(CHUNK_BYTES):
            hashed.update(block)
            size += len(block)
    return hashed.hexdigest(), size


@contextmanager
def verified_copy(root, name, expected, policy):
    # Private, short-lived snapshot avoids both whole-file RAM loading and a
    # hash/response race. No copy is retained after the request ends.
    with evidence_stream(root, name) as source:
        size = os.fstat(source.fileno()).st_size
        require_free_space(tempfile.gettempdir(), policy, additional_bytes=size)
        with tempfile.TemporaryFile() as frozen:
            hashed = hashlib.sha256()
            while block := source.read(CHUNK_BYTES):
                hashed.update(block)
                frozen.write(block)
            require(hashed.hexdigest() == expected, "HISTORY_RESULT_CONFLICT", "Published result differs")
            frozen.flush()
            frozen.seek(0)
            yield frozen


def project_json(stream, fields):
    """Select top-level fields without decoding unneeded log strings/arrays.

    Only used after the complete publication's SHA has been checked. A read-only
    mapping lets the OS page large skipped values instead of allocating them as
    Python objects. Selected values retain the normal JSON decoder's semantics.
    """
    try:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            return _project(raw, fields)
    except MemoryError as exc:
        raise DomainError("HISTORY_MEMORY_UNAVAILABLE", "Insufficient memory to read selected result fields", 503) from exc


def _project(raw, fields):
    length = len(raw)

    def space(pos):
        while pos < length and raw[pos] in b" \r\n\t":
            pos += 1
        return pos

    def string_end(pos):
        pos += 1
        while True:
            match = _STRING.search(raw, pos)
            if match is None:
                raise ValueError("Unterminated JSON string")
            pos = match.end()
            if match[0] == b'"':
                return pos
            pos += 1  # Escaped quote/backslash cannot terminate this string.

    def value_end(pos):
        if pos >= length:
            raise ValueError("Missing JSON value")
        if raw[pos] == 34:
            return string_end(pos)
        if raw[pos] not in b"[{":
            found = _DELIMITER.search(raw, pos)
            return found.start() if found else length
        stack = []
        while match := _TOKEN.search(raw, pos):
            token, pos = match[0], match.end()
            if token == b'"':
                pos = string_end(match.start())
            elif token in (b"{", b"["):
                stack.append(token)
            elif not stack or stack.pop() != {b"}": b"{", b"]": b"["}[token]:
                raise ValueError("Unbalanced JSON container")
            elif not stack:
                return pos
        raise ValueError("Unterminated JSON container")

    pos, selected, seen = space(0), {}, set()
    if pos == length or raw[pos] != 123:
        raise ValueError("Result must be a JSON object")
    pos = space(pos + 1)
    while pos < length and raw[pos] != 125:
        if raw[pos] != 34:
            raise ValueError("Expected JSON field")
        end = string_end(pos)
        key = json.loads(raw[pos:end])
        if key in seen:
            raise ValueError("Duplicate JSON field")
        seen.add(key)
        pos = space(end)
        if pos >= length or raw[pos] != 58:
            raise ValueError("Expected JSON colon")
        pos = space(pos + 1)
        end = value_end(pos)
        if end == pos:
            raise ValueError("Missing JSON value")
        if key in fields:
            selected[key] = json.loads(raw[pos:end])
        pos = space(end)
        if pos < length and raw[pos] == 44:
            pos = space(pos + 1)
            if pos >= length or raw[pos] == 125:
                raise ValueError("Trailing JSON comma")
        elif pos >= length or raw[pos] != 125:
            raise ValueError("Expected JSON separator")
    if pos >= length or space(pos + 1) != length:
        raise ValueError("Incomplete JSON object")
    return selected


_STRING = re.compile(rb'["\\]')
_TOKEN = re.compile(rb'["{}\[\]]')
_DELIMITER = re.compile(rb'[\s,}]')
