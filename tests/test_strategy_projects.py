"""Real project filesystem transactions in disposable state, without running strategies."""

import errno
import os
import shutil
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.strategies import projects as project_module
from ashare_quant.strategies.files import StrategyFiles
from ashare_quant.strategies.projects import ProjectFiles, validate_project_sources
from tests.test_strategy_files import PREFIX, client, configuration, error  # noqa: F401


@pytest.fixture
def projects(tmp_path):
    return ProjectFiles(tmp_path.resolve())


def save(projects, current, sources, entrypoint=None):
    return projects.save(current["path"], current["strategy_id"], entrypoint or current["entrypoint"],
                         sources, current["revision"])


def test_plain_project_save_move_reopen_and_source_delete(projects):
    folders = StrategyFiles(projects.files.state_root)
    folders.mkdir("研究/日频")
    folders.mkdir("归档/子类")
    current = projects.create("研究/日频/动量")
    assert current["files"] == {"main.py": ""}
    descriptor = tomllib.loads((projects.root / current["path"] / "aquant.toml").read_text())
    assert descriptor == {"format_version": 1, "project_id": current["strategy_id"], "entrypoint": "main.py"}
    sources = {"main.py": "from .signals import 值\r\n", "signals/__init__.py": "值=1\n",
               "signals/rank.py": "def unfinished("}
    edited = save(projects, current, sources)
    assert edited["revision"] != current["revision"]
    assert edited["files"] == sources
    assert (projects.root / edited["path"] / "main.py").read_bytes() == sources["main.py"].encode()
    moved = projects.move(edited["path"], "归档/子类/新名", edited["strategy_id"], edited["revision"])
    assert moved == {**edited, "path": "归档/子类/新名"}
    assert projects.read_id(edited["strategy_id"]) == moved
    assert not (projects.root / edited["path"]).exists()
    assert folders.list("归档/子类")["entries"] == [
        {"name": "新名", "path": moved["path"], "kind": "project", "strategy_id": moved["strategy_id"]},
    ]
    renamed = save(projects, moved, {"runner.py": "from .utils import value\n", "utils.py": "value = 2"}, "runner.py")
    assert sorted(p.name for p in (projects.root / moved["path"]).iterdir()) == ["aquant.toml", "runner.py", "utils.py"]
    assert ProjectFiles(projects.files.state_root).read_id(moved["strategy_id"]) == renamed


def test_old_interfaces_cannot_enter_project_or_create_nested_projects(projects):
    current = projects.create("one")
    legacy = StrategyFiles(projects.files.state_root)
    for action in (
        lambda: legacy.read("one/main.py"), lambda: legacy.save("one/main.py", "bad", overwrite=True),
        lambda: legacy.save("one/new.py", "bad"), lambda: legacy.list("one"),
        lambda: legacy.mkdir("one/nested"), lambda: projects.create("one/nested"),
        lambda: projects.move("one", "one/nested", current["strategy_id"], current["revision"]),
    ):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PROJECT_REQUIRED"
    assert projects.read("one") == current
    assert sorted(p.name for p in (projects.root / "one").iterdir()) == ["aquant.toml", "main.py"]
    legacy.save("standalone.py", "still works")
    assert legacy.read("standalone.py")["source"] == "still works"


@pytest.mark.parametrize("sources,entrypoint", [
    ({"../main.py": ""}, "../main.py"), ({"/main.py": ""}, "/main.py"),
    ({"pkg\\main.py": ""}, "pkg\\main.py"), ({"main.PY": ""}, "main.PY"),
    ({"my-file.py": ""}, "my-file.py"), ({"class.py": ""}, "class.py"),
    ({"pkg with spaces/main.py": ""}, "pkg with spaces/main.py"),
    ({"__pycache__/main.py": ""}, "__pycache__/main.py"),
    ({"main.py": "", "MAIN.py": ""}, "main.py"),
    ({"main.py": "", "Sig/a.py": "", "sig/b.py": ""}, "main.py"),
    ({"main.py": "", "signals.py": "", "signals/a.py": ""}, "main.py"),
    ({"main.py": "", "Ｓignal.py": ""}, "main.py"),
    ({"main.py": "", "aquant.toml": ""}, "main.py"),
    ({"helper.py": ""}, "missing.py"), ({}, "main.py"),
])
def test_invalid_snapshots_never_create_files(projects, sources, entrypoint):
    with pytest.raises(DomainError):
        validate_project_sources(entrypoint, sources)
    assert not projects.root.exists()


def test_package_entrypoint_unicode_and_runtime_module_names_are_legal(projects):
    current = projects.create("入口", entrypoint="包/__init__.py")
    sources = {"包/__init__.py": "from .信号 import x", "包/信号.py": "x=1", "aquant.py": "", "os.py": ""}
    assert save(projects, current, sources)["files"] == sources


def test_revision_conflict_and_concurrent_saves_preserve_winner(projects):
    current = projects.create("one")
    ready = Barrier(2)

    def writer(value):
        ready.wait()
        try:
            return save(projects, current, {"main.py": value})
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, ["first", "second"]))
    assert outcomes.count("STRATEGY_PROJECT_REVISION_CONFLICT") == 1
    winner = next(item for item in outcomes if isinstance(item, dict))
    assert projects.read("one") == winner
    for action in (
        lambda: save(projects, current, {"main.py": "stale"}),
        lambda: projects.move("one", "moved", current["strategy_id"], current["revision"]),
    ):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PROJECT_REVISION_CONFLICT"
    assert projects.read("one") == winner


def test_external_edit_is_detected_before_exchange(projects, monkeypatch):
    current = projects.create("one")
    original = project_module.atomic

    def edit(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        if name == "main.py":
            (projects.root / "one/main.py").write_text("external editor")
        return result
    monkeypatch.setattr(project_module, "atomic", edit)
    with pytest.raises(DomainError) as refused:
        save(projects, current, {"main.py": "browser"})
    assert refused.value.code == "STRATEGY_PROJECT_REVISION_CONFLICT"
    assert projects.read("one")["files"] == {"main.py": "external editor"}
    assert sorted(p.name for p in projects.root.iterdir()) == ["one"]


@pytest.mark.parametrize("stage", ["source", "staging_sync", "exchange"])
def test_failed_transaction_keeps_original_and_no_temporary_tree(projects, monkeypatch, stage):
    current = projects.create("one")
    original_sync = os.fsync

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "controlled failure")

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            fail()
        original_sync(fd)
    with monkeypatch.context() as patch:
        if stage == "source":
            patch.setattr(project_module, "atomic", fail)
        elif stage == "staging_sync":
            patch.setattr(os, "fsync", sync)
        else:
            patch.setattr(project_module, "rename_directory", fail)
        with pytest.raises(DomainError) as refused:
            save(projects, current, {"main.py": "updated", "new.py": ""})
    assert refused.value.code == "STORAGE_FULL"
    assert refused.value.details["save_state"] == "not_saved"
    assert projects.read("one") == current
    assert sorted(p.name for p in projects.root.iterdir()) == ["one"]


def test_published_but_unsynced_save_is_uncertain_and_keeps_recovery_copy(projects, monkeypatch):
    current = projects.create("one")
    exchanged = False
    original_rename, original_sync = project_module.rename_directory, os.fsync

    def rename(*args, **kwargs):
        nonlocal exchanged
        original_rename(*args, **kwargs)
        exchanged = True

    def sync(fd):
        if exchanged:
            raise OSError(errno.EIO, "controlled durability failure")
        original_sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(project_module, "rename_directory", rename)
        patch.setattr(os, "fsync", sync)
        with pytest.raises(DomainError) as refused:
            save(projects, current, {"main.py": "updated", "helper.py": "new"})
    assert refused.value.code == "STRATEGY_SAVE_UNCERTAIN"
    assert refused.value.details["save_state"] == "uncertain"
    assert projects.read_id(current["strategy_id"])["files"] == {"main.py": "updated", "helper.py": "new"}
    recovery = projects.root / refused.value.details["recovery_directory"]
    assert (recovery / "main.py").read_text() == ""
    assert [item["name"] for item in StrategyFiles(projects.files.state_root).list()["entries"]] == ["one"]


def test_duplicate_ids_are_never_assigned_arbitrarily(projects):
    current = projects.create("one")
    shutil.copytree(projects.root / "one", projects.root / "two")
    for action in (lambda: projects.read_id(current["strategy_id"]), lambda: projects.read("one"),
                   lambda: save(projects, current, {"main.py": "no"}),
                   lambda: projects.move("one", "three", current["strategy_id"], current["revision"])):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PROJECT_ID_CONFLICT"
    assert (projects.root / "one/main.py").read_text() == (projects.root / "two/main.py").read_text() == ""


@pytest.mark.parametrize("kind", ["source_symlink", "source_hardlink", "descriptor_symlink", "directory_symlink"])
def test_project_links_fail_closed_without_touching_outside(projects, tmp_path, kind):
    current = projects.create("one")
    outside = tmp_path / "private.py"
    outside.write_text("private")
    target = projects.root / "one"
    if kind == "descriptor_symlink":
        (target / "aquant.toml").unlink()
        (target / "aquant.toml").symlink_to(outside)
    elif kind == "directory_symlink":
        (target / "pkg").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "source_hardlink":
        os.link(outside, target / "linked.py")
    else:
        (target / "linked.py").symlink_to(outside)
    with pytest.raises(DomainError) as refused:
        save(projects, current, {"main.py": "no"})
    assert refused.value.code == "STRATEGY_PATH_UNSAFE"
    assert outside.read_text() == "private"
    assert (target / "main.py").read_text() == ""


def test_unknown_files_and_nested_descriptor_are_never_discarded(projects):
    current = projects.create("one")
    for name in ("notes.txt", "aquant.toml"):
        nested = projects.root / "one/pkg"
        nested.mkdir(exist_ok=True)
        (nested / name).write_text("retain me")
        with pytest.raises(DomainError) as refused:
            save(projects, current, {"main.py": "no"})
        assert refused.value.code == "STRATEGY_PROJECT_SOURCE_INVALID"
        assert (nested / name).read_text() == "retain me"
        (nested / name).unlink()


def test_case_unicode_and_destination_conflicts_preserve_projects(projects):
    current = projects.create("Momentum")
    for path in ("momentum", "Ｍomentum"):
        with pytest.raises(DomainError) as refused:
            projects.create(path)
        assert refused.value.code == "STRATEGY_NAME_CONFLICT"
    other = projects.create("other")
    with pytest.raises(DomainError):
        projects.move("Momentum", "other", current["strategy_id"], current["revision"])
    with pytest.raises(DomainError):
        projects.move("Momentum", "missing/one", current["strategy_id"], current["revision"])
    assert projects.read("Momentum") == current and projects.read("other") == other


def test_case_only_project_rename_retains_identity(projects):
    current = projects.create("Momentum")
    moved = projects.move("Momentum", "momentum", current["strategy_id"], current["revision"])
    assert moved == {**current, "path": "momentum"}
    assert os.listdir(projects.root) == ["momentum"]
    assert projects.read_id(current["strategy_id"]) == moved


def test_unsupported_atomic_exchange_has_no_partial_save(projects, monkeypatch):
    current = projects.create("one")

    def unavailable(*args, **kwargs):
        raise DomainError("STRATEGY_PROJECT_ATOMIC_UNAVAILABLE", "unsupported filesystem", 503)
    monkeypatch.setattr(project_module, "rename_directory", unavailable)
    with pytest.raises(DomainError) as refused:
        save(projects, current, {"main.py": "new", "helper.py": ""})
    assert refused.value.code == "STRATEGY_PROJECT_ATOMIC_UNAVAILABLE"
    assert projects.read("one") == current
    assert os.listdir(projects.root) == ["one"]


@pytest.mark.parametrize("name", ["aquant.toml", "AQUANT.TOML", ".aquant-recovery", ".AQUANT-recovery"])
def test_system_names_cannot_be_reused_or_read_through_aliases(projects, name):
    for action in (lambda: projects.create(name), lambda: projects.read(name),
                   lambda: projects.files.mkdir(name)):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PATH_INVALID"
    assert not projects.root.exists()


def test_policy_budgets_replace_single_file_256_kib_limit(projects, tmp_path):
    current = projects.create("large")
    large = "#" * 300_000
    assert save(projects, current, {"main.py": large})["files"]["main.py"] == large
    files = StrategyFiles(tmp_path.resolve())
    files.save("legacy.py", large)
    assert files.read("legacy.py")["source"] == large
    small = ProjectFiles(tmp_path.resolve(), total_bytes=1200)
    current = small.create("small")
    for sources, code in (({"main.py": "中" * 401}, "STRATEGY_PROJECT_SIZE"),
                          ({"main.py": "#" * 700, "helper.py": "#" * 700}, "STRATEGY_PROJECT_SIZE")):
        with pytest.raises(DomainError) as refused:
            save(small, current, sources)
        assert refused.value.code == code
    assert small.read("small") == current


def test_project_http_full_roundtrip_and_strict_query(client):  # noqa: F811
    created = client.post(PREFIX + "/projects", json={"path": "策略"})
    assert created.status_code == 200, created.text
    current = created.json()
    payload = {key: current[key] for key in ("path", "strategy_id", "entrypoint", "files")}
    payload.update(files={"main.py": "from .helper import n", "helper.py": "n=1"}, expected_revision=current["revision"])
    response = client.put(PREFIX + "/projects", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    error(client.put(PREFIX + "/projects", json=payload), 409, "STRATEGY_PROJECT_REVISION_CONFLICT")
    moved = client.post(PREFIX + "/projects/move", json={"path": "策略", "destination": "改名",
                        "strategy_id": saved["strategy_id"], "expected_revision": saved["revision"]})
    assert moved.status_code == 200, moved.text
    assert client.get(PREFIX + "/projects", params={"strategy_id": current["strategy_id"]}).json() == moved.json()
    for query in ("", "?path=改名&strategy_id=" + current["strategy_id"], "?path=改名&path=改名", "?root=/tmp"):
        error(client.get(PREFIX + "/projects" + query), 422, "STRATEGY_QUERY_UNSUPPORTED")
    error(client.put(PREFIX + "/files", json={"path": "改名/helper.py", "source": "bypass", "overwrite": True}),
          409, "STRATEGY_PROJECT_REQUIRED")
    assert client.app.state.python_jobs.repo.rows() == []
