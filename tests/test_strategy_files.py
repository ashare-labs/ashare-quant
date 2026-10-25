"""Strategy saves use disposable deployment state, never live files or a worker."""

import errno
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.paper.service import PaperService
from ashare_quant.storage import artifacts
from ashare_quant.storage.store import Store
from ashare_quant.strategies.files import StrategyFiles

PREFIX = "/api/v1/python"


@pytest.fixture
def configuration(tmp_path):
    document = {
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "daily": {"kind": "local_daily", "path": "daily", "role": "market"},
            "release": {"kind": "store", "path": "release", "role": "publication"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "daily", "release_store_asset": "release",
            "control_db": "state/control.sqlite3", "paper_root": "state/paper",
        }},
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = resolve(path)
    Store(config.control_db)
    PaperService(config.paper_root)
    return config


@pytest.fixture
def client(configuration):
    with TestClient(create_app(product_configuration=configuration)) as value:
        yield value


@pytest.fixture
def files(tmp_path):
    return StrategyFiles(tmp_path.resolve())


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["Cache-Control"] == "no-store"


def test_directory_and_utf8_round_trip_survive_new_process(client, configuration):
    root = configuration.control_db.parent / "strategies"
    initial = client.get(PREFIX + "/directories")
    assert initial.json() == {"root": str(root), "path": "", "entries": []}
    assert not root.exists()
    for _ in range(2):
        assert client.post(PREFIX + "/directories", json={"path": "研究/日频"}).status_code == 200
    path = "研究/日频/动量.py"
    source = "# 中文和 emoji 📈\r\nraise RuntimeError('must never run on save')\r\n"
    result = client.put(PREFIX + "/files", json={"path": path, "source": source})
    assert result.status_code == 200 and result.json() == {"path": path, "strategy_id": path}
    assert (root / path).read_bytes() == source.encode("utf-8")
    assert client.get(PREFIX + "/files", params={"path": path}).json() == {"path": path, "strategy_id": path, "source": source}
    (root / "研究/日频/not-a-strategy.txt").write_text("ignored")
    assert client.get(PREFIX + "/directories", params={"path": "研究"}).json()["entries"] == [
        {"name": "日频", "path": "研究/日频", "kind": "directory"},
    ]
    assert client.get(PREFIX + "/directories", params={"path": "研究/日频"}).json()["entries"] == [
        {"name": "动量.py", "path": path, "strategy_id": path, "kind": "file"},
    ]
    program = """
import json, sys
from fastapi.testclient import TestClient
from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
with TestClient(create_app(product_configuration=resolve(sys.argv[1]))) as client:
    response = client.get('/api/v1/python/files', params={'path': sys.argv[2]})
    assert response.status_code == 200, response.text
    print(json.dumps(response.json()))
"""
    reopened = subprocess.run(
        [sys.executable, "-B", "-c", program, str(configuration.deployment), path],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(reopened.stdout) == {"path": path, "strategy_id": path, "source": source}
    assert client.app.state.python_jobs.repo.rows() == []
    assert client.app.state.store.strategies() == []


def test_explicit_overwrite_and_missing_parent(client, configuration):
    payload = {"path": "blank.py", "source": ""}
    assert client.put(PREFIX + "/files", json=payload).status_code == 200
    changed = {**payload, "source": "def incomplete("}
    error(client.put(PREFIX + "/files", json=changed), 409, "STRATEGY_FILE_EXISTS")
    assert client.get(PREFIX + "/files", params={"path": "blank.py"}).json()["source"] == ""
    assert client.put(PREFIX + "/files", json={**changed, "overwrite": True}).status_code == 200
    assert client.get(PREFIX + "/files", params={"path": "blank.py"}).json()["source"] == changed["source"]
    error(client.put(PREFIX + "/files", json={"path": "absent/nested/a.py", "source": ""}),
          404, "STRATEGY_NOT_FOUND")
    assert not (configuration.control_db.parent / "strategies/absent").exists()
    error(client.get(PREFIX + "/files", params={"path": "absent.py"}), 404, "STRATEGY_NOT_FOUND")
    error(client.post(PREFIX + "/directories", json={"path": "blank.py/child"}),
          409, "STRATEGY_PATH_CONFLICT")


@pytest.mark.parametrize("path", [
    "../escape.py", "/escape.py", "a/../../escape.py", "./a.py", "a//b.py", "a/",
    "a/./b.py", "a\\b.py", "C:/a.py", "a\x00b.py", "a\nb.py", "a" * 256 + ".py",
])
def test_path_boundaries_do_not_create_or_touch_files(files, path):
    for action in (lambda: files.mkdir(path), lambda: files.list(path),
                   lambda: files.read(path), lambda: files.save(path, "untrusted")):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PATH_INVALID"
    assert not files.root.exists()


@pytest.mark.parametrize("path", ["", "strategy.txt", "strategy.PY", "folder"])
def test_only_lowercase_python_files(files, path):
    for action in (lambda: files.read(path), lambda: files.save(path, "")):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PATH_INVALID"


@pytest.mark.parametrize("target", ["root", "directory", "file", "broken", "hardlink"])
def test_links_are_refused_without_modifying_the_target(files, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "secret.py"
    original.write_bytes(b"private original")
    files.root.mkdir()
    path = "alias/secret.py" if target == "directory" else "secret.py"
    if target == "root":
        files.root.rmdir()
        files.root.symlink_to(outside, target_is_directory=True)
    elif target == "directory":
        (files.root / "alias").symlink_to(outside, target_is_directory=True)
    elif target == "hardlink":
        os.link(original, files.root / path)
    else:
        (files.root / path).symlink_to(original if target == "file" else outside / "missing.py")
    for action in (lambda: files.read(path), lambda: files.save(path, "changed", overwrite=True)):
        with pytest.raises(DomainError) as refused:
            action()
        assert refused.value.code == "STRATEGY_PATH_UNSAFE"
    if target in {"root", "directory"}:
        with pytest.raises(DomainError):
            files.mkdir("alias/new" if target == "directory" else "new")
    if target != "root":
        assert files.list()["entries"] == []
    assert original.read_bytes() == b"private original"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.py"]


def test_symlink_ancestor_of_state_root_is_refused(tmp_path):
    outside = tmp_path / "outside"
    (outside / "state").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError) as refused:
        StrategyFiles(alias / "state").save("a.py", "")
    assert refused.value.code == "STRATEGY_PATH_UNSAFE"
    assert not (outside / "state/strategies").exists()


def test_directory_swap_cannot_redirect_save(files, tmp_path, monkeypatch):
    files.mkdir("folder")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = False

    def swap(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == "folder" and not swapped:
            swapped = True
            (files.root / "folder").rename(files.root / "original-folder")
            (files.root / "folder").symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(DomainError) as refused:
        files.save("folder/a.py", "escape")
    assert refused.value.code in {"STRATEGY_PATH_UNSAFE", "STRATEGY_PATH_CONFLICT"}
    assert list(outside.iterdir()) == []
    assert list((files.root / "original-folder").iterdir()) == []


def test_nonregular_and_non_utf8_files_are_not_read(files):
    files.mkdir("directory.py")
    os.mkfifo(files.root / "pipe.py")
    for path in ("directory.py", "pipe.py"):
        for action in (lambda: files.read(path), lambda: files.save(path, "", overwrite=True)):
            with pytest.raises(DomainError) as refused:
                action()
            assert refused.value.code == "STRATEGY_PATH_CONFLICT"
    (files.root / "bad.py").write_bytes(b"\xff")
    with pytest.raises(DomainError) as refused:
        files.read("bad.py")
    assert refused.value.code == "STRATEGY_SOURCE_ENCODING"
    with pytest.raises(DomainError) as refused:
        files.save("bad.py", "\ud800", overwrite=True)
    assert refused.value.code == "STRATEGY_SOURCE_ENCODING"
    assert (files.root / "bad.py").read_bytes() == b"\xff"


def test_single_file_has_no_retired_size_gate(tmp_path):
    files = StrategyFiles(tmp_path.resolve())
    source = "#" * (64 * 1024 * 1024 + 1)
    files.save("large.py", source)
    assert files.read("large.py")["source"] == source
    files.save("large.py", "#中文", overwrite=True)
    assert files.read("large.py")["source"] == "#中文"


def test_long_filename_fits_even_with_atomic_temporary_file(files):
    path = "a" * 252 + ".py"
    files.save(path, "# first\n")
    files.save(path, "# replaced\n", overwrite=True)
    assert files.read(path)["source"] == "# replaced\n"
    assert [entry["path"] for entry in files.list()["entries"]] == [path]


@pytest.mark.parametrize("stage,number,code", [
    ("replace", errno.ENOSPC, "STORAGE_FULL"),
    ("replace", errno.EACCES, "STORAGE_PERMISSION_DENIED"),
    ("replace", errno.EROFS, "STORAGE_PERMISSION_DENIED"),
    ("file_sync", errno.EIO, "STORAGE_UNAVAILABLE"),
])
def test_overwrite_failure_preserves_original_and_cleans_pending(
    client, configuration, monkeypatch, stage, number, code,
):
    path = "safe.py"
    assert client.put(PREFIX + "/files", json={"path": path, "source": "original"}).status_code == 200
    real_sync = os.fsync

    def fail(*args, **kwargs):
        raise OSError(number, "controlled failure")

    def sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            fail()
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace" if stage == "replace" else "fsync", fail if stage == "replace" else sync)
        response = client.put(PREFIX + "/files", json={"path": path, "source": "changed", "overwrite": True})
    error(response, 503, code)
    assert response.json()["error"]["details"]["save_state"] == "not_saved"
    root = configuration.control_db.parent / "strategies"
    assert (root / path).read_text() == "original"
    assert sorted(p.name for p in root.iterdir()) == [path]


def test_directory_sync_failure_reports_uncertain_publication(client, monkeypatch):
    payload = {"path": "sync.py", "source": "old"}
    assert client.put(PREFIX + "/files", json=payload).status_code == 200
    real_sync = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "controlled directory sync failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        response = client.put(PREFIX + "/files", json={**payload, "source": "new", "overwrite": True})
    error(response, 503, "STRATEGY_SAVE_UNCERTAIN")
    assert response.json()["error"]["details"]["save_state"] == "uncertain"
    assert client.get(PREFIX + "/files", params={"path": "sync.py"}).json()["source"] == "new"


def test_concurrent_create_never_silently_overwrites(files):
    files.mkdir("shared")
    ready = Barrier(2)

    def save(source):
        ready.wait()
        try:
            files.save("shared/one.py", source)
            return source
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ["first", "second"]))
    assert outcomes.count("STRATEGY_FILE_EXISTS") == 1
    assert files.read("shared/one.py")["source"] in outcomes
    assert sorted(p.name for p in (files.root / "shared").iterdir()) == ["one.py"]


def test_create_while_another_publication_is_finishing_is_a_conflict(files, monkeypatch):
    files.mkdir("shared")
    published, finish = Event(), Event()
    real_link = os.link

    def link(*args, **kwargs):
        real_link(*args, **kwargs)
        published.set()
        assert finish.wait(5)

    monkeypatch.setattr(os, "link", link)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(files.save, "shared/one.py", "first")
        try:
            assert published.wait(5)
            with pytest.raises(DomainError) as refused:
                files.save("shared/one.py", "second")
            assert refused.value.code == "STRATEGY_FILE_EXISTS"
        finally:
            finish.set()
        first.result()
    assert files.read("shared/one.py")["source"] == "first"


def test_http_validation_origin_and_configuration_fail_closed(client, configuration):
    payload = {"path": "a.py", "source": ""}
    error(client.put(PREFIX + "/files", json=payload, headers={"Origin": "https://external.example"}),
          403, "M2_JOB_ORIGIN_REFUSED")
    for value in ({**payload, "root": "/tmp"}, {**payload, "overwrite": "true"}):
        assert client.put(PREFIX + "/files", json=value).status_code == 422
    error(client.get(PREFIX + "/directories", params={"root": "/tmp"}),
          422, "STRATEGY_QUERY_UNSUPPORTED")
    error(client.get(PREFIX + "/directories?path=a&path=b"), 422, "STRATEGY_QUERY_UNSUPPORTED")
    error(client.put(PREFIX + "/files", content=b'{"path":"a.py","source":"\\ud800"}',
                     headers={"Content-Type": "application/json"}), 422, "STRATEGY_SOURCE_ENCODING")
    error(client.get(PREFIX + "/files?path=%2E%2E%2Foutside.py"), 422, "STRATEGY_PATH_INVALID")
    assert not (configuration.control_db.parent / "strategies").exists()
    document = json.loads(configuration.deployment.read_text())
    document["profiles"]["product"]["control_db"] = "another/control.sqlite3"
    configuration.deployment.write_text(json.dumps(document))
    error(client.put(PREFIX + "/files", json=payload), 409, "PRODUCT_CONFIGURATION_CHANGED")
    assert not (configuration.control_db.parent / "strategies").exists()


def test_no_unconfigured_working_directory_fallback(tmp_path):
    with TestClient(create_app(str(tmp_path / "control.sqlite3"))) as client:
        error(client.put(PREFIX + "/files", json={"path": "a.py", "source": ""}),
              503, "DEPLOYMENT_NOT_CONFIGURED")
    assert not (tmp_path / "strategies").exists()


def test_shared_atomic_json_and_create_only_compatibility(tmp_path):
    path = tmp_path / "artifact.json"
    artifacts.atomic(path, {"original": "中文"})
    assert json.loads(path.read_bytes()) == {"original": "中文"}
    artifacts.atomic(path, b"replacement")
    with pytest.raises(FileExistsError):
        artifacts.atomic(path, b"not saved", overwrite=False)
    assert path.read_bytes() == b"replacement"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact.json"]
