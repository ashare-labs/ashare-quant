"""Plain UTF-8 strategy files inside the configured control database's state root."""

import errno
import fcntl
import os
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import AtomicWriteUncertain, atomic
from ashare_quant.storage.errors import storage_error

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
PROJECT_DESCRIPTOR = "aquant.toml"
INTERNAL_PREFIX = ".aquant-"


def parts(path, *, file=False):
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DomainError("STRATEGY_PATH_INVALID", "Use a UTF-8 relative path", 422) from exc
    members = path.split("/") if path else []
    if (
        len(encoded) > 1024
        or any(c in path for c in ("\\", ":"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(p in {"", ".", ".."} or name_key(p).startswith(INTERNAL_PREFIX)
               or name_key(p) == PROJECT_DESCRIPTOR
               or len(p.encode("utf-8")) > 255 for p in members)
        or (file and (not members or not members[-1].endswith(".py")))
    ):
        raise DomainError(
            "STRATEGY_PATH_INVALID", "Use a relative directory path or a lowercase .py filename; "
            "absolute paths, empty/dot components, backslashes and system-reserved names are forbidden", 422,
        )
    return members


def regular(info):
    if stat.S_ISLNK(info.st_mode):
        raise DomainError("STRATEGY_PATH_UNSAFE", "Linked strategy files are not supported", 403)
    if not stat.S_ISREG(info.st_mode):
        raise DomainError("STRATEGY_PATH_CONFLICT", "Expected a regular .py file", 409)
    if info.st_nlink != 1:
        raise DomainError("STRATEGY_PATH_UNSAFE", "Linked strategy files are not supported", 403)


def source_bytes(source):
    try:
        raw = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DomainError("STRATEGY_SOURCE_ENCODING", "Source is not valid UTF-8", 422) from exc
    return raw


@contextmanager
def operation(path, name):
    try:
        yield
    except OSError as exc:
        if isinstance(exc, AtomicWriteUncertain):
            raise DomainError(
                "STRATEGY_SAVE_UNCERTAIN",
                "File was published but durability is unconfirmed; reopen it before retrying", 503,
                details={"path": path, "operation": name, "save_state": "uncertain"},
            ) from exc
        if exc.errno == errno.ENOENT:
            error = DomainError("STRATEGY_NOT_FOUND", "Strategy file or directory does not exist", 404)
        elif exc.errno == errno.ELOOP:
            error = DomainError("STRATEGY_PATH_UNSAFE", "Symbolic links are not supported", 403)
        elif exc.errno in {errno.ENOTDIR, errno.EISDIR}:
            error = DomainError("STRATEGY_PATH_CONFLICT", "A path component has the wrong type", 409)
        elif exc.errno == errno.EEXIST:
            error = DomainError("STRATEGY_FILE_EXISTS", "File exists; explicitly set overwrite=true", 409)
        else:
            error = storage_error(exc)
        raise DomainError(error.code, error.message, error.status, details={
            "path": path, "operation": name,
            **({"save_state": "not_saved"} if name == "save" else {}),
        }) from exc


def open_directory(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    # lstat distinguishes links from wrong-type components on both macOS and Linux.
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise DomainError("STRATEGY_PATH_UNSAFE", "Symbolic links are not supported", 403)
    return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)


def stored_name(parent, requested, info):
    """Use the actual directory entry, not a case/Unicode normalization guess."""
    names = os.listdir(parent)
    for name in ([requested] if requested in names else names):
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if os.path.samestat(current, info):
            return name
    raise DomainError("STRATEGY_PATH_CHANGED", "Path changed during access; reopen it", 409)


def name_key(name):
    """Reject names that would alias on common case/Unicode-normalizing filesystems."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def unique_name(parent, requested, *, existing=None):
    for name in os.listdir(parent):
        if name_key(name) == name_key(requested) and name != existing:
            if name == requested:
                raise DomainError("STRATEGY_FILE_EXISTS", "A file or directory already uses this name", 409)
            raise DomainError("STRATEGY_NAME_CONFLICT", "A case or Unicode equivalent name exists", 409,
                              details={"name": requested, "existing": name})


def classification(fd):
    try:
        os.stat(PROJECT_DESCRIPTOR, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise DomainError("STRATEGY_PROJECT_REQUIRED", "Use the whole-project interface for project files", 409)


class StrategyFiles:
    def __init__(self, state_root):
        self.state_root = Path(state_root)
        if not self.state_root.is_absolute():
            raise ValueError("Strategy storage requires the resolved deployment state root")
        self.root = self.state_root / "strategies"

    @contextmanager
    def state(self):
        fd = os.open(self.state_root.anchor, DIRECTORY_FLAGS)
        try:
            for member in self.state_root.parts[1:]:
                child = open_directory(fd, member)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def lock(self, *, exclusive=False):
        # The existing deployment directory is the lock inode; reads create no files.
        # Single-file writers share this lock, retaining their atomic create semantics.
        with self.state() as fd:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield

    @contextmanager
    def directory(self, members, *, create=False, create_root=False, resolved=None,
                  project=False):
        # Walk from / without following links, including ancestors of the configured root.
        fd = os.open(self.state_root.anchor, DIRECTORY_FLAGS)
        try:
            for member in self.state_root.parts[1:]:
                child = open_directory(fd, member)
                os.close(fd)
                fd = child
            child = open_directory(fd, "strategies", create=create or create_root)
            os.close(fd)
            fd = child
            for member in members:
                classification(fd)
                if create:
                    try:
                        info = os.stat(member, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        unique_name(fd, member)
                    else:
                        actual = stored_name(fd, member, info)
                        unique_name(fd, member, existing=actual)
                child = open_directory(fd, member, create=create)
                if resolved is not None:
                    try:
                        resolved.append(stored_name(fd, member, os.fstat(child)))
                    except BaseException:
                        os.close(child)
                        raise
                os.close(fd)
                fd = child
            if not project:
                classification(fd)
            yield fd
        finally:
            os.close(fd)

    def list(self, path=""):
        members = parts(path)
        entries, resolved = [], []
        with operation(path, "list"), self.lock():
            try:
                with self.directory(members, resolved=resolved) as fd:
                    for name in os.listdir(fd):
                        relative = "/".join([*resolved, name])
                        try:
                            parts(relative)
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except (DomainError, FileNotFoundError):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                            from ashare_quant.strategies.projects import descriptor
                            child = open_directory(fd, name)
                            try:
                                metadata = descriptor(child, optional=True)
                            finally:
                                os.close(child)
                            if metadata is not None:
                                kind = "project"
                        elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and name.endswith(".py")):
                            kind = "file"
                        else:
                            continue
                        entries.append({"name": name, "path": relative, "kind": kind,
                                        **({"strategy_id": relative} if kind == "file" else {}),
                                        **({"strategy_id": metadata["project_id"]} if kind == "project" else {})})
            except FileNotFoundError:
                if members or not self.state_root.is_dir():
                    raise
                # An unused deployment has an empty strategy directory; GET creates nothing.
        return {"root": str(self.root), "path": "/".join(resolved),
                "entries": sorted(entries, key=lambda item: (item["kind"] != "directory", item["name"]))}

    def mkdir(self, path):
        members = parts(path)
        if not members:
            raise DomainError("STRATEGY_PATH_INVALID", "Specify a nonempty directory path", 422)
        resolved = []
        with operation(path, "mkdir"), self.lock(), self.directory(members, create=True, resolved=resolved):
            return {"path": "/".join(resolved)}

    def read(self, path):
        members = parts(path, file=True)
        resolved = []
        with operation(path, "read"), self.lock(), self.directory(members[:-1], resolved=resolved) as directory:
            # Nonblocking open plus fstat prevents a FIFO or device masquerading as a file.
            regular(os.stat(members[-1], dir_fd=directory, follow_symlinks=False))
            fd = os.open(members[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                regular(info)
                path = "/".join([*resolved, stored_name(directory, members[-1], info)])
                parts(path, file=True)
                raw = stream.read()
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("STRATEGY_SOURCE_ENCODING", "File is not valid UTF-8", 422) from exc
        return {"path": path, "strategy_id": path, "source": source}

    def save(self, path, source, *, overwrite=False):
        members = parts(path, file=True)
        raw = source_bytes(source)
        resolved = []
        with operation(path, "save"), self.lock(), self.directory(
            members[:-1], create_root=True, resolved=resolved,
        ) as directory:
            name = members[-1]
            try:
                info = os.stat(members[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                unique_name(directory, name)
            else:
                if not overwrite and stat.S_ISREG(info.st_mode):
                    # A concurrent create may still have its temporary hard link attached.
                    raise DomainError("STRATEGY_FILE_EXISTS", "File exists; explicitly set overwrite=true", 409)
                regular(info)
                name = stored_name(directory, name, info)
                parts("/".join([*resolved, name]), file=True)
            atomic(name, raw, dir_fd=directory, overwrite=overwrite)
            actual = stored_name(directory, name, os.stat(name, dir_fd=directory, follow_symlinks=False))
            path = "/".join([*resolved, actual])
        return {"path": path, "strategy_id": path}
