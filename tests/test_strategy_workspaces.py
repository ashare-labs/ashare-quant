"""Opaque edge-selected storage scopes; these are not Python sandboxes."""

import os

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.strategies.files import StrategyFiles
from ashare_quant.strategies.projects import ProjectFiles
from tests.support.strategy_files import client as client
from tests.support.strategy_files import configuration as configuration

A, B = "a" * 32, "b" * 32
PREFIX = "/api/v1/python"


def test_independent_files_and_projects_never_import_source(tmp_path):
    legacy = StrategyFiles(tmp_path)
    first, second = (StrategyFiles(tmp_path, workspace=value) for value in (A, B))
    for files in (legacy, first, second):
        assert files.list()["entries"] == []
    assert list(tmp_path.iterdir()) == []
    for name, files in (("legacy", legacy), ("first", first), ("second", second)):
        files.save("same.py", f"raise RuntimeError('{name}: must never execute')")
        assert name in files.read("same.py")["source"]
    first_projects = ProjectFiles(tmp_path, workspace=A)
    second_projects = ProjectFiles(tmp_path, workspace=B)
    project = first_projects.create("alpha", "main.py")
    assert first_projects.read_id(project["strategy_id"]) == project
    with pytest.raises(DomainError) as refused:
        second_projects.read_id(project["strategy_id"])
    assert refused.value.status == 404
    other = second_projects.create("alpha", "main.py")
    assert other["strategy_id"] != project["strategy_id"]
    assert len(legacy.list()["entries"]) == 1


@pytest.mark.parametrize("value", ["", "../strategies", "a/b", "A" * 32, "0" * 31, "0" * 33])
def test_namespace_is_never_a_path(tmp_path, value):
    with pytest.raises(DomainError) as refused:
        StrategyFiles(tmp_path, workspace=value)
    assert refused.value.code == "STRATEGY_WORKSPACE_INVALID"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("level", ["workspaces", "workspace", "strategies", "file", "hardlink"])
def test_scope_links_cannot_reach_another_scope(tmp_path, level):
    victim = StrategyFiles(tmp_path, workspace=B)
    victim.save("private.py", "secret")
    attacker = StrategyFiles(tmp_path, workspace=A)
    if level == "workspaces":
        (tmp_path / "workspaces").rename(tmp_path / "moved")
        (tmp_path / "workspaces").symlink_to(tmp_path / "moved")
    elif level == "workspace":
        (tmp_path / "workspaces" / A).symlink_to(tmp_path / "workspaces" / B)
    else:
        attacker.root.parent.mkdir()
        if level == "strategies":
            attacker.root.symlink_to(victim.root)
        else:
            attacker.root.mkdir()
            if level == "file":
                (attacker.root / "private.py").symlink_to(victim.root / "private.py")
            else:
                os.link(victim.root / "private.py", attacker.root / "private.py")
    with pytest.raises(DomainError) as refused:
        attacker.save("private.py", "stolen", overwrite=True)
    assert refused.value.code == "STRATEGY_PATH_UNSAFE"
    original = tmp_path / "moved" / B / "strategies/private.py" if level == "workspaces" else victim.root / "private.py"
    assert original.read_text() == "secret"


def test_api_advertises_scope_contract_and_enforces_every_file_route(client, configuration):
    assert client.get(PREFIX + "/workspace-capabilities").json()["contract"] == "aq.strategy-workspaces.v1"
    first, second = {"X-Quant-Workspace": A}, {"X-Quant-Workspace": B}
    assert client.put(PREFIX + "/files", headers=first, json={"path": "a.py", "source": "private"}).status_code == 200
    assert client.get(PREFIX + "/files?path=a.py", headers=second).status_code == 404
    assert client.get(PREFIX + "/files?path=a.py").status_code == 404
    project = client.post(PREFIX + "/projects", headers=first, json={"path": "project"}).json()
    assert client.get(PREFIX + "/projects", headers=second, params={"strategy_id": project["strategy_id"]}).status_code == 404
    assert client.post(PREFIX + "/directories", headers=second, json={"path": "empty"}).status_code == 200
    assert [entry["path"] for entry in client.get(PREFIX + "/directories", headers=second).json()["entries"]] == ["empty"]
    assert client.get(PREFIX + "/directories", headers={"X-Quant-Workspace": "../"}).status_code == 422
    assert client.get(PREFIX + "/directories", headers=[("X-Quant-Workspace", A), ("X-Quant-Workspace", B)]).status_code == 422
    assert client.app.state.python_jobs.repo.rows() == []
