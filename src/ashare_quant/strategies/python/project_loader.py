"""Load one immutable strategy snapshot as an isolated, ordinary Python package.

This is import isolation for trusted local strategies, not a Python sandbox. Only
the manifest's modules belong to the package; third-party imports still use the
worker's existing environment. No strategy directory is added to ``sys.path``.
"""

import builtins
import importlib
import importlib.abc
import importlib.util
import keyword
import re
import sys
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

from ashare_quant.storage.artifacts import require
from ashare_quant.strategies.imports import strategy_import


class FrozenProject(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """A context-managed package whose source is restricted to a frozen file list."""

    def __init__(self, root, project):
        if hasattr(project, "model_dump"):
            project = project.model_dump(mode="json")
        require(isinstance(project, dict), "PYTHON_PROJECT_INVALID")
        require(
            re.fullmatch(r"aqproj-[0-9a-f]{32}", str(project.get("project_id"))) is not None,
            "PYTHON_PROJECT_INVALID",
        )
        files = project.get("files")
        require(isinstance(files, dict) and bool(files), "PYTHON_PROJECT_INVALID")
        self.root = Path(root) / "source"
        self.name = "_aquant_project_" + uuid.uuid4().hex
        self.files = {}
        self.modules = {}
        self.packages = {self.name}
        self.namespace = None
        self.active = False
        path_names = {}
        for relative, source in files.items():
            require(isinstance(relative, str) and isinstance(source, str), "PYTHON_PROJECT_INVALID")
            path = PurePosixPath(relative)
            require(
                str(path) == relative and not path.is_absolute() and path.suffix == ".py"
                and "\\" not in relative
                and all(
                    part.isidentifier() and not keyword.iskeyword(part)
                    and part != "__pycache__"
                    and unicodedata.normalize("NFKC", part) == part
                    for part in (*path.parts[:-1], path.stem)
                ),
                "PYTHON_PROJECT_INVALID",
                f"Invalid project source path: {relative}",
            )
            for index, part in enumerate(path.parts):
                key = tuple(piece.casefold() for piece in path.parts[:index + 1])
                require(
                    key not in path_names or path_names[key] == part,
                    "PYTHON_PROJECT_INVALID", "Ambiguous project source names",
                )
                path_names[key] = part
            parts = path.parts[:-1] if path.name == "__init__.py" else (*path.parts[:-1], path.stem)
            name = ".".join((self.name, *parts))
            require(name not in self.modules, "PYTHON_PROJECT_INVALID", "Conflicting module paths")
            self.modules[name] = relative
            self.files[relative] = source.encode("utf-8")
            for size in range(len(path.parts)):
                self.packages.add(".".join((self.name, *path.parts[:size])))
        for name in self.packages:
            relative = next((path for module, path in self.modules.items() if module.casefold() == name.casefold()), None)
            require(
                relative is None or PurePosixPath(relative).name == "__init__.py",
                "PYTHON_PROJECT_INVALID",
                "A module and a package cannot have the same name",
            )
        self.entrypoint = project.get("entrypoint")
        require(self.entrypoint in self.files, "PYTHON_PROJECT_INVALID", "Project entrypoint is missing")
        self.entry_module = next(name for name, path in self.modules.items() if path == self.entrypoint)

    def read(self, relative):
        path = self.root / relative
        require(
            not self.root.is_symlink() and self.root.is_dir()
            and all(not parent.is_symlink() for parent in path.parents if parent.is_relative_to(self.root))
            and not path.is_symlink() and path.is_file(),
            "PYTHON_PROJECT_CHANGED", f"Frozen project source is missing or linked: {relative}",
        )
        source = path.read_bytes()
        require(source == self.files[relative], "PYTHON_PROJECT_CHANGED", f"Frozen project source changed: {relative}")
        return source

    def verify(self):
        """Check every source, including modules that the strategy never imports."""
        require(
            self.root.is_dir() and not self.root.is_symlink(),
            "PYTHON_PROJECT_CHANGED", "Frozen project source directory is unavailable",
        )
        found = set()
        for path in self.root.rglob("*"):
            require(not path.is_symlink(), "PYTHON_PROJECT_CHANGED", "Frozen project contains a link")
            if path.is_dir():
                continue
            # macOS can enumerate decomposed Unicode spellings for the same
            # canonical filename. The snapshot keeps its normalized Python name.
            relative = unicodedata.normalize("NFC", path.relative_to(self.root).as_posix())
            require(
                path.is_file() and relative in self.files and relative not in found,
                "PYTHON_PROJECT_CHANGED", f"Unlisted frozen project source: {relative}",
            )
            self.read(relative)
            found.add(relative)
        require(found == self.files.keys(), "PYTHON_PROJECT_CHANGED", "Frozen project source is missing")

    def __enter__(self):
        self.verify()
        require(not self.active, "PYTHON_PROJECT_INVALID", "Project loader is already active")
        self.active = True
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in tuple(sys.modules):
            if name == self.name or name.startswith(self.name + "."):
                del sys.modules[name]
        self.active = False
        self.namespace = None

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.name and not fullname.startswith(self.name + "."):
            return None
        if fullname not in self.modules and fullname not in self.packages:
            # Never let PathFinder discover extra files in this package's __path__.
            raise ModuleNotFoundError(f"No frozen project module named {fullname!r}", name=fullname)
        relative = self.modules.get(fullname)
        if relative is None:
            spec = importlib.util.spec_from_loader(fullname, self, is_package=True)
            spec.submodule_search_locations = [str(self.root.joinpath(*fullname.split(".")[1:]))]
            return spec
        return importlib.util.spec_from_file_location(
            fullname, self.root / relative, loader=self,
            submodule_search_locations=[str((self.root / relative).parent)]
            if fullname in self.packages else None,
        )

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        relative = self.modules.get(module.__name__)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": strategy_import}
        if module.__name__ == self.entry_module and self.namespace:
            module.__dict__.update({
                key: value for key, value in self.namespace.items()
                if key not in {"__name__", "__package__", "__spec__", "__file__", "__loader__", "__path__", "__cached__"}
            })
        if relative is not None:
            # Recheck at deferred import time as well as at run boundaries. Compile
            # verified bytes directly, never a previously cached .pyc.
            source = self.read(relative)
            exec(compile(source, str(self.root / relative), "exec"), module.__dict__)

    def load(self, namespace):
        require(self.active, "PYTHON_PROJECT_INVALID", "Project loader is not active")
        self.namespace = namespace
        return importlib.import_module(self.entry_module).__dict__
