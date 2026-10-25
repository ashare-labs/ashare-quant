"""Complete Python project transactions over ordinary, inspectable directories."""

import ctypes
import errno
import json
import keyword
import os
import shutil
import stat
import sys
import tomllib
import unicodedata
import uuid
from contextlib import contextmanager

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import AtomicWriteUncertain, atomic, digest
from ashare_quant.strategies.files import (
    INTERNAL_PREFIX,
    PROJECT_DESCRIPTOR,
    StrategyFiles,
    name_key,
    open_directory,
    operation,
    parts,
    regular,
    source_bytes,
    unique_name,
)
from ashare_quant.strategies.identity import is_project_id
from ashare_quant.tasks.policy import ServicePolicy


def invalid(message):
    return DomainError("STRATEGY_PROJECT_SOURCE_INVALID", message, 422)


def module_name(member):
    return (member.isidentifier() and not keyword.iskeyword(member)
            and member != "__pycache__" and unicodedata.normalize("NFKC", member) == member)


def validate_project_sources(entrypoint, files, *, total_bytes=None):
    """Validate a snapshot without importing code, changing names, or checking syntax.

    Limits are service budgets supplied by the caller, not project-count limits.
    Empty/incomplete source is deliberately editable. All imports still execute
    only inside a job, where the frozen source is a private Python package.
    """
    if not isinstance(files, dict) or not files or not isinstance(entrypoint, str):
        raise invalid("A project needs an entrypoint and a nonempty source mapping")
    names, modules, directories, total = {}, set(), set(), 0
    for path, source in files.items():
        if not isinstance(path, str) or not isinstance(source, str):
            raise invalid("Project source paths and source contents must be strings")
        members = parts(path, file=True)
        identifiers = [*members[:-1], members[-1][:-3]]
        if any(not module_name(member) for member in identifiers):
            raise invalid("Source directories and module names must be unambiguous Python identifiers")
        for index, member in enumerate(members):
            key = tuple(name_key(value) for value in members[:index + 1])
            previous = names.setdefault(key, tuple(members[:index + 1]))
            if previous != tuple(members[:index + 1]):
                raise invalid("Source paths collide after Unicode or case normalization")
        modules.add(tuple(name_key(member) for member in identifiers))
        directories.update(tuple(name_key(member) for member in identifiers[:index])
                           for index in range(1, len(identifiers)))
        total += len(source_bytes(source))
        if total_bytes is not None and total > total_bytes:
            raise DomainError("STRATEGY_PROJECT_SIZE", "Project exceeds the service artifact_bytes budget", 422,
                              details={"artifact_bytes": total_bytes})
    if modules.intersection(directories):
        raise invalid("A Python module and a package directory cannot have the same name")
    if entrypoint not in files:
        raise invalid("The entrypoint must identify one of the project source files")
    return files


def read_bytes(fd, name):
    regular(os.stat(name, dir_fd=fd, follow_symlinks=False))
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(child, "rb") as stream:
        regular(os.fstat(stream.fileno()))
        raw = stream.read()
    return raw


def descriptor(fd, *, optional=False):
    try:
        raw = read_bytes(fd, PROJECT_DESCRIPTOR)
    except FileNotFoundError:
        if optional:
            return None
        raise DomainError("STRATEGY_PROJECT_NOT_FOUND", "Directory is not a strategy project", 404) from None
    try:
        value = tomllib.loads(raw.decode("utf-8"))
        if (set(value) != {"format_version", "project_id", "entrypoint"}
                or type(value["format_version"]) is not int or value["format_version"] != 1
                or not is_project_id(value["project_id"]) or not isinstance(value["entrypoint"], str)):
            raise ValueError("descriptor fields")
        validate_project_sources(value["entrypoint"], {value["entrypoint"]: ""})
    except (UnicodeDecodeError, ValueError, DomainError) as exc:
        raise DomainError("STRATEGY_PROJECT_DESCRIPTOR_INVALID", "aquant.toml is not a supported project descriptor", 422) from exc
    return value


def descriptor_bytes(project_id, entrypoint):
    return ("format_version = 1\n"
            f"project_id = {json.dumps(project_id, ensure_ascii=False)}\n"
            f"entrypoint = {json.dumps(entrypoint, ensure_ascii=False)}\n").encode("utf-8")


def rename_directory(source_fd, source, target_fd, target, *, exchange=False):
    """No two-rename gap: macOS and Linux supply atomic exchange/no-clobber."""
    libc = ctypes.CDLL(None, use_errno=True)
    name = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    try:
        rename = getattr(libc, name)
    except AttributeError:
        raise DomainError("STRATEGY_PROJECT_ATOMIC_UNAVAILABLE", "This filesystem platform has no atomic project rename", 503) from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    flags = 2 if exchange else (4 if sys.platform == "darwin" else 1)
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise DomainError("STRATEGY_PROJECT_ATOMIC_UNAVAILABLE", "The filesystem does not support atomic project transactions", 503)
        raise OSError(code, os.strerror(code))


class ProjectFiles:
    def __init__(self, state_root, *, total_bytes=None, workspace=None):
        policy = ServicePolicy()
        self.files = StrategyFiles(state_root, workspace=workspace)
        self.total_bytes = policy.artifact_bytes if total_bytes is None else total_bytes
        self.root = self.files.root

    def _sources(self, fd, prefix="", budget=None):
        budget = [0] if budget is None else budget
        result, names = {}, set()
        for name in sorted(os.listdir(fd)):
            if not prefix and name == PROJECT_DESCRIPTOR:
                continue
            if name_key(name) == PROJECT_DESCRIPTOR:
                raise invalid("Project descriptors cannot be nested or aliased")
            relative = prefix + unicodedata.normalize("NFC", name)
            parts(relative)
            key = name_key(name)
            if key in names:
                raise invalid("Source names collide after Unicode or case normalization")
            names.add(key)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if not module_name(unicodedata.normalize("NFC", name)):
                    raise invalid("Project source directories must be Python package identifiers")
                child = open_directory(fd, name)
                try:
                    children = self._sources(child, relative + "/", budget)
                    if result.keys() & children.keys():
                        raise invalid("Source paths collide after Unicode normalization")
                    result.update(children)
                finally:
                    os.close(child)
            else:
                regular(info)
                if not name.endswith(".py"):
                    raise invalid("Project directories may contain only aquant.toml and Python source files")
                try:
                    raw = read_bytes(fd, name)
                    budget[0] += len(raw)
                    if budget[0] > self.total_bytes:
                        raise DomainError("STRATEGY_PROJECT_SIZE", "Project exceeds the service artifact_bytes budget", 422,
                                          details={"artifact_bytes": self.total_bytes})
                    value = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DomainError("STRATEGY_SOURCE_ENCODING", "File is not valid UTF-8", 422) from exc
                if relative in result:
                    raise invalid("Source paths collide after Unicode normalization")
                result[relative] = value
        return result

    def _read(self, path):
        members, resolved = parts(path), []
        if not members:
            raise invalid("Specify a project directory")
        with self.files.directory(members, project=True, resolved=resolved) as fd:
            metadata = descriptor(fd)
            sources = self._sources(fd)
            validate_project_sources(metadata["entrypoint"], sources,
                                     total_bytes=self.total_bytes)
        snapshot = {"project_id": metadata["project_id"], "entrypoint": metadata["entrypoint"], "files": sources}
        return {"path": "/".join(resolved), "strategy_id": metadata["project_id"],
                "entrypoint": metadata["entrypoint"], "files": sources, "revision": digest(snapshot)}

    def _locations(self, project_id):
        result = []

        def visit(fd, prefix):
            for name in os.listdir(fd):
                if name_key(name).startswith(INTERNAL_PREFIX):
                    continue
                relative = "/".join([*prefix, name])
                try:
                    parts(relative)
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except (DomainError, FileNotFoundError):
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                child = open_directory(fd, name)
                try:
                    metadata = descriptor(child, optional=True)
                    if metadata is None:
                        visit(child, [*prefix, name])
                    elif metadata["project_id"] == project_id:
                        result.append(relative)
                finally:
                    os.close(child)
        try:
            with self.files.directory([]) as fd:
                visit(fd, [])
        except FileNotFoundError:
            return []
        if len(result) > 1:
            raise DomainError("STRATEGY_PROJECT_ID_CONFLICT", "More than one project has this stable identity", 409,
                              details={"strategy_id": project_id, "paths": sorted(result)})
        return result

    def read(self, path):
        with operation(path, "read"), self.files.lock():
            result = self._read(path)
            self._locations(result["strategy_id"])
            return result

    def read_id(self, project_id):
        if not is_project_id(project_id):
            raise invalid("A stable project identity is required")
        with operation(project_id, "read"), self.files.lock():
            locations = self._locations(project_id)
            if not locations:
                raise DomainError("STRATEGY_PROJECT_NOT_FOUND", "No saved project has this identity", 404)
            return self._read(locations[0])

    @contextmanager
    def _stage(self, parent, project_id, entrypoint, sources):
        name = INTERNAL_PREFIX + uuid.uuid4().hex
        os.mkdir(name, mode=0o700, dir_fd=parent)
        fd = open_directory(parent, name)
        retained = [False]
        try:
            try:
                for relative, source in sorted(sources.items()):
                    members = parts(relative, file=True)
                    current = os.dup(fd)
                    try:
                        for component in members[:-1]:
                            child = open_directory(current, component, create=True)
                            os.close(current)
                            current = child
                        atomic(members[-1], source_bytes(source),
                               dir_fd=current, overwrite=False)
                    finally:
                        os.close(current)
                atomic(PROJECT_DESCRIPTOR, descriptor_bytes(project_id, entrypoint), dir_fd=fd, overwrite=False)
                os.fsync(fd)
            except AtomicWriteUncertain as exc:
                # A temporary source was published, but no project transaction was.
                raise OSError(exc.errno, "Project staging failed before publication") from exc
            yield name, retained
        finally:
            os.close(fd)
            if not retained[0]:
                try:
                    shutil.rmtree(name, dir_fd=parent)
                except OSError:
                    pass  # Failed cleanup cannot change a known publication outcome.

    def create(self, path, entrypoint="main.py"):
        members, resolved = parts(path), []
        if not members:
            raise invalid("Specify a new project directory")
        sources = {entrypoint: ""}
        validate_project_sources(entrypoint, sources, total_bytes=self.total_bytes)
        with operation(path, "create"), self.files.lock(exclusive=True), self.files.directory(
            members[:-1], create_root=True, resolved=resolved,
        ) as parent:
            unique_name(parent, members[-1])
            project_id = "aqproj-" + uuid.uuid4().hex
            if self._locations(project_id):
                raise DomainError("STRATEGY_PROJECT_ID_CONFLICT", "Generated project identity already exists", 409)
            with self._stage(parent, project_id, entrypoint, sources) as (stage, retained):
                rename_directory(parent, stage, parent, members[-1])
                try:
                    os.fsync(parent)
                except OSError as exc:
                    raise AtomicWriteUncertain(exc.errno, "Project created; durability unconfirmed") from exc
            return self._read("/".join([*resolved, members[-1]]))

    def _expected(self, path, strategy_id, expected_revision):
        current = self._read(path)
        if current["strategy_id"] != strategy_id:
            raise DomainError("STRATEGY_PROJECT_ID_CONFLICT", "Project identity differs; reopen the project", 409)
        self._locations(strategy_id)
        if current["revision"] != expected_revision:
            raise DomainError("STRATEGY_PROJECT_REVISION_CONFLICT", "Project changed; reopen it before retrying", 409,
                              details={"revision": current["revision"]})
        return current

    def save(self, path, strategy_id, entrypoint, files, expected_revision):
        validate_project_sources(entrypoint, files, total_bytes=self.total_bytes)
        with operation(path, "save"), self.files.lock(exclusive=True):
            current = self._expected(path, strategy_id, expected_revision)
            members = parts(current["path"])
            with self.files.directory(members[:-1]) as parent:
                with self._stage(parent, strategy_id, entrypoint, files) as (stage, retained):
                    # Catch edits made by external local editors while building the transaction.
                    self._expected(current["path"], strategy_id, expected_revision)
                    rename_directory(parent, stage, parent, members[-1], exchange=True)
                    retained[0] = True
                    try:
                        os.fsync(parent)
                    except OSError as exc:
                        retained[0] = True  # The old tree remains an explicit recovery copy.
                        raise DomainError(
                            "STRATEGY_SAVE_UNCERTAIN", "Project published; reopen to confirm its revision", 503,
                            details={"path": path, "operation": "save", "save_state": "uncertain",
                                     "recovery_directory": "/".join([*members[:-1], stage])},
                        ) from exc
                    retained[0] = False
            return self._read(current["path"])

    def move(self, path, destination, strategy_id, expected_revision):
        destination_members, resolved = parts(destination), []
        if not destination_members:
            raise invalid("Specify a destination project directory")
        with operation(path, "move"), self.files.lock(exclusive=True):
            current = self._expected(path, strategy_id, expected_revision)
            members = parts(current["path"])
            if destination == current["path"]:
                return current
            with self.files.directory(members[:-1]) as source, self.files.directory(
                destination_members[:-1], resolved=resolved,
            ) as target:
                same_parent = os.path.samestat(os.fstat(source), os.fstat(target))
                unique_name(target, destination_members[-1], existing=members[-1] if same_parent else None)
                rename_directory(source, members[-1], target, destination_members[-1])
                try:
                    os.fsync(target)
                    os.fsync(source)
                except OSError as exc:
                    raise AtomicWriteUncertain(exc.errno, "Project moved; durability unconfirmed") from exc
            return self._read("/".join([*resolved, destination_members[-1]]))
