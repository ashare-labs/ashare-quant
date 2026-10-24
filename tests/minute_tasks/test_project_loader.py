import importlib
import sys
import unicodedata
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.strategies.python.project_loader import FrozenProject
from ashare_quant.tasks.minute.runner import source_location


def freeze(root, files, entrypoint="main.py"):
    for relative, source in files.items():
        path = root / "source" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return {"project_id": "aqproj-" + "1" * 32, "entrypoint": entrypoint, "files": files}


def test_package_metadata_relative_circular_imports_and_library_names(tmp_path, monkeypatch):
    api = ModuleType("aquant")
    api.g = SimpleNamespace(answer=42)
    monkeypatch.setitem(sys.modules, "aquant", api)
    files = {
        "__init__.py": "package_value = 3\n",
        "main.py": '''
import math
import aquant
from . import math as local_math, aquant as local_api, jqdata as local_legacy_name
from .signals.rules import answer
from .cycle import first
from . import package_value
assert math.sqrt(9) == 3 and local_math.value == 7
assert aquant.g is shared_g and local_api.value == 11
assert local_legacy_name.value == 17
assert answer() == 42 and first.answer() == 13 and package_value == 3
assert __spec__.name == __name__ and __spec__.origin == __file__
assert __package__ == __name__.rsplit('.', 1)[0]
''',
        "math.py": "value = 7\n",
        "aquant.py": "value = 11\n",
        "jqdata.py": "value = 17\n",
        "signals/rules.py": "from aquant import g\nassert 'shared_g' not in globals()\ndef answer(): return g.answer\n",
        "cycle/__init__.py": "from . import first\n",
        "cycle/first.py": "from . import second\ndef answer(): return second.answer()\n",
        "cycle/second.py": "from . import first\ndef answer(): return 13\n",
    }
    project = freeze(tmp_path, files)
    before_path = sys.path.copy()
    loader = FrozenProject(tmp_path, project)
    with loader:
        namespace = loader.load({"shared_g": api.g})
        assert namespace["__file__"] == str(tmp_path / "source" / "main.py")
        assert namespace["answer"].__module__ == loader.name + ".signals.rules"
        assert sys.modules[loader.name + ".signals"].__spec__.submodule_search_locations
        assert importlib.reload(sys.modules[loader.name + ".math"]).value == 7
        assert sys.path == before_path
    assert not any(name == loader.name or name.startswith(loader.name + ".") for name in sys.modules)
    assert loader not in sys.meta_path and sys.path == before_path


def test_same_named_helpers_in_two_projects_and_entry_init(tmp_path):
    one = FrozenProject(tmp_path / "one", freeze(tmp_path / "one", {"main.py": "from .helper import value\n", "helper.py": "value = 1\n"}))
    two = FrozenProject(tmp_path / "two", freeze(tmp_path / "two", {"__init__.py": "from .helper import value\n", "helper.py": "value = 2\n"}, "__init__.py"))
    with one, two:
        assert one.load({})["value"] == 1
        assert two.load({})["value"] == 2
        assert one.name != two.name
    assert one.name + ".helper" not in sys.modules
    assert two.name + ".helper" not in sys.modules


@pytest.mark.parametrize("files", [
    {"../main.py": "pass"}, {"/main.py": "pass"}, {"a//main.py": "pass"},
    {"a\\main.py": "pass"}, {"class.py": "pass"}, {"K.py": "pass"},
    {"Main.py": "pass", "main.py": "pass"},
    {"helper.py": "pass", "helper/a.py": "pass"},
    {"Helper.py": "pass", "helper/a.py": "pass"},
    {"helper.py": "pass", "helper/__init__.py": "pass"},
])
def test_invalid_or_ambiguous_import_paths_are_rejected(tmp_path, files):
    with pytest.raises(DomainError) as error:
        FrozenProject(tmp_path, {"project_id": "aqproj-" + "1" * 32, "entrypoint": next(iter(files)), "files": files})
    assert error.value.code == "PYTHON_PROJECT_INVALID"


@pytest.mark.parametrize("change", ["edit", "missing", "unlisted", "symlink"])
def test_entire_snapshot_checked_before_execution(tmp_path, change):
    project = freeze(tmp_path, {"main.py": "pass\n", "unused.py": "pass\n"})
    unused = tmp_path / "source" / "unused.py"
    if change == "edit":
        unused.write_text("changed = True\n")
    elif change == "missing":
        unused.unlink()
    elif change == "unlisted":
        (tmp_path / "source" / "extra.py").write_text("pass\n")
    else:
        unused.unlink()
        unused.symlink_to(tmp_path / "source" / "main.py")
    with pytest.raises(DomainError) as error, FrozenProject(tmp_path, project):
        pytest.fail("Changed snapshot was accepted")
    assert error.value.code == "PYTHON_PROJECT_CHANGED"


def test_deferred_import_rechecks_source_and_does_not_discover_extra_files(tmp_path):
    project = freeze(tmp_path, {"main.py": "pass\n", "helper.py": "value = 1\n"})
    loader = FrozenProject(tmp_path, project)
    with loader:
        loader.load({})
        (tmp_path / "source" / "extra.py").write_text("raise AssertionError('executed')\n")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(loader.name + ".extra")
        (tmp_path / "source" / "extra.py").unlink()
        (tmp_path / "source" / "helper.py").write_text("value = 2\n")
        with pytest.raises(DomainError) as error:
            importlib.import_module(loader.name + ".helper")
        assert error.value.code == "PYTHON_PROJECT_CHANGED"
        with pytest.raises(DomainError):
            loader.verify()


@pytest.mark.parametrize("body, expected_line", [("def fail():\n    raise ValueError('helper failed')\n", 2), ("def fail(:\n    pass\n", 1)])
def test_failure_location_and_module_cleanup(tmp_path, body, expected_line):
    project = freeze(tmp_path, {"main.py": "from .nested.helper import fail\nfail()\n", "nested/helper.py": body})
    loader = FrozenProject(tmp_path, project)
    with pytest.raises((ValueError, SyntaxError)) as error, loader:
        loader.load({})
    assert source_location(error.value, tmp_path) == ("nested/helper.py", expected_line)
    assert not any(name.startswith(loader.name) for name in sys.modules)
    assert loader not in sys.meta_path


def test_legacy_import_diagnostic_in_helper_and_single_file_location(tmp_path):
    project = freeze(tmp_path, {"main.py": "from . import helper\n", "helper.py": "import jqdata\n"})
    loader = FrozenProject(tmp_path, project)
    with pytest.raises(ModuleNotFoundError, match="AQUANT_LEGACY_IMPORT") as error, loader:
        loader.load({})
    assert source_location(error.value, tmp_path) == ("helper.py", 1)
    try:
        exec(compile("raise ValueError('old strategy')", str(tmp_path / "strategy.py"), "exec"), {})
    except ValueError as exc:
        assert source_location(exc, tmp_path) == ("strategy.py", 1)
    assert not Path(tmp_path / "source" / "__pycache__").exists()


def test_unicode_snapshot_names_follow_filesystem_canonical_equivalence(tmp_path):
    project = freeze(tmp_path, {
        "main.py": "from .café.résumé import value\n",
        "café/résumé.py": "value = 42\n",
    })
    original = tmp_path / "source/café"
    decomposed = original.with_name(unicodedata.normalize("NFD", original.name))
    original.rename(decomposed)
    member = decomposed / "résumé.py"
    member.rename(member.with_name(unicodedata.normalize("NFD", member.name)))
    loader = FrozenProject(tmp_path, project)
    if (original / "résumé.py").is_file():
        # APFS/HFS+: the canonical spelling still identifies the same file.
        with loader:
            assert loader.load({})["value"] == 42
    else:
        # Filesystems without that equivalence now lack the manifest's path.
        with pytest.raises(DomainError), loader:
            pytest.fail("Missing canonical snapshot path was accepted")


def test_distinct_files_cannot_share_a_normalized_snapshot_name(tmp_path):
    project = freeze(tmp_path, {"main.py": "pass\n", "café.py": "value = 42\n"})
    decomposed = tmp_path / "source" / unicodedata.normalize("NFD", "café.py")
    decomposed.write_text("value = 42\n")
    loader = FrozenProject(tmp_path, project)
    if len(list((tmp_path / "source").iterdir())) == len(project["files"]):
        # A normalization-insensitive filesystem has only the original file.
        with loader:
            loader.load({})
    else:
        with pytest.raises(DomainError), loader:
            pytest.fail("Duplicate normalized source paths were accepted")
