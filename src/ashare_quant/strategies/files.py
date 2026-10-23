"""Plain UTF-8 strategy files inside the configured control database's state root."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import AtomicWriteUncertain, atomic
from ashare_quant.storage.errors import storage_error

MAX_SOURCE_BYTES = 262144  # Same source budget as Python job admission.
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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
        or any(p in {"", ".", ".."} or len(p.encode("utf-8")) > 255 for p in members)
        or (file and (not members or not members[-1].endswith(".py")))
    ):
        raise DomainError(
            "STRATEGY_PATH_INVALID", "Use a relative directory path or a lowercase .py filename; "
            "absolute paths, empty components, dot components and backslashes are forbidden", 422,
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
    if len(raw) > MAX_SOURCE_BYTES:
        raise DomainError("STRATEGY_SOURCE_SIZE", "Source must fit 256 KiB of UTF-8", 422)
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


class StrategyFiles:
    def __init__(self, state_root):
        self.state_root = Path(state_root)
        if not self.state_root.is_absolute():
            raise ValueError("Strategy storage requires the resolved deployment state root")
        self.root = self.state_root / "strategies"

    @contextmanager
    def directory(self, members, *, create=False, create_root=False):
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
                child = open_directory(fd, member, create=create)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def list(self, path=""):
        members = parts(path)
        entries = []
        with operation(path, "list"):
            try:
                with self.directory(members) as fd:
                    for name in os.listdir(fd):
                        relative = "/".join([*members, name])
                        try:
                            parts(relative)
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except (DomainError, FileNotFoundError):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                        elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and name.endswith(".py")):
                            kind = "file"
                        else:
                            continue
                        entries.append({"name": name, "path": relative, "kind": kind})
            except FileNotFoundError:
                if members or not self.state_root.is_dir():
                    raise
                # An unused deployment has an empty strategy directory; GET creates nothing.
        return {"root": str(self.root), "path": path,
                "entries": sorted(entries, key=lambda item: (item["kind"] != "directory", item["name"]))}

    def mkdir(self, path):
        members = parts(path)
        if not members:
            raise DomainError("STRATEGY_PATH_INVALID", "Specify a nonempty directory path", 422)
        with operation(path, "mkdir"), self.directory(members, create=True):
            return {"path": path}

    def read(self, path):
        members = parts(path, file=True)
        with operation(path, "read"), self.directory(members[:-1]) as directory:
            # Nonblocking open plus fstat prevents a FIFO or device masquerading as a file.
            regular(os.stat(members[-1], dir_fd=directory, follow_symlinks=False))
            fd = os.open(members[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                regular(os.fstat(stream.fileno()))
                raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise DomainError("STRATEGY_SOURCE_SIZE", "Source must fit 256 KiB of UTF-8", 422)
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("STRATEGY_SOURCE_ENCODING", "File is not valid UTF-8", 422) from exc
        return {"path": path, "source": source}

    def save(self, path, source, *, overwrite=False):
        members = parts(path, file=True)
        raw = source_bytes(source)
        with operation(path, "save"), self.directory(members[:-1], create_root=True) as directory:
            try:
                info = os.stat(members[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not overwrite and stat.S_ISREG(info.st_mode):
                    # A concurrent create may still have its temporary hard link attached.
                    raise DomainError("STRATEGY_FILE_EXISTS", "File exists; explicitly set overwrite=true", 409)
                regular(info)
            atomic(members[-1], raw, dir_fd=directory, overwrite=overwrite)
        return {"path": path}
