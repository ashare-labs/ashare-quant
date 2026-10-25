"""Directory and source file operations; saving never submits or imports Python code."""

from typing import Literal

from fastapi import Query, Request
from pydantic import Field, field_validator, model_validator

from ashare_quant.contracts import Contract, DomainError
from ashare_quant.strategies.files import StrategyFiles, parts, source_bytes, validate_workspace
from ashare_quant.strategies.identity import PROJECT_ID_PATTERN, StrategyID
from ashare_quant.strategies.projects import ProjectFiles, validate_project_sources


class StrategyPath(Contract):
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path", mode="before")
    @classmethod
    def relative_path(cls, value):
        if isinstance(value, str):
            parts(value)
        return value


class StrategySave(StrategyPath):
    source: str
    overwrite: bool = Field(default=False, strict=True)

    @field_validator("source", mode="before")
    @classmethod
    def utf8_source(cls, value):
        if isinstance(value, str):
            source_bytes(value)
        return value


class StrategyFilePath(StrategyPath):
    strategy_id: StrategyID


class StrategySource(StrategyFilePath):
    source: str


class StrategyEntry(StrategyPath):
    name: str
    kind: Literal["directory", "file", "project"]
    strategy_id: StrategyID | None = Field(default=None, exclude_if=lambda value: value is None)


class StrategyDirectory(Contract):
    root: str
    path: str
    entries: list[StrategyEntry]


class ProjectCreate(StrategyPath):
    entrypoint: str = "main.py"

    @field_validator("entrypoint")
    @classmethod
    def entry_source(cls, value):
        validate_project_sources(value, {value: ""})
        return value


class ProjectSource(ProjectCreate):
    strategy_id: str = Field(pattern=PROJECT_ID_PATTERN)
    files: dict[str, str]
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectSave(ProjectCreate):
    strategy_id: str = Field(pattern=PROJECT_ID_PATTERN)
    files: dict[str, str]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def source_mapping(self):
        validate_project_sources(self.entrypoint, self.files)
        return self


class ProjectMove(StrategyPath):
    destination: str = Field(min_length=1, max_length=1024)
    strategy_id: str = Field(pattern=PROJECT_ID_PATTERN)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("destination")
    @classmethod
    def destination_path(cls, value):
        parts(value)
        return value


def workspace_handle(request):
    values = request.headers.getlist("x-quant-workspace")
    if len(values) > 1:
        raise DomainError("STRATEGY_WORKSPACE_INVALID", "Use one workspace handle", 422)
    return validate_workspace(values[0] if values else None)


def mount(app, product):
    prefix = "/api/v1/python"

    @app.get(prefix + "/workspace-capabilities")
    def workspace_capabilities():
        return {"contract": "aq.strategy-workspaces.v1", "header": "X-Quant-Workspace",
                "legacy_root": "header_absent", "execution_isolation": False}

    def validate_query(request, allowed):
        if (set(request.query_params) - allowed
                or any(len(request.query_params.getlist(field)) > 1 for field in allowed)):
            raise DomainError("STRATEGY_QUERY_UNSUPPORTED", "Use only the documented fields", 422)

    def files(request, *, query=False):
        validate_query(request, {"path"} if query else set())
        return StrategyFiles(product.configured().control_db.parent, workspace=workspace_handle(request))

    def projects(request, *, query=False):
        validate_query(request, {"path", "strategy_id"} if query else set())
        policy = app.state.python_jobs.repo.policy
        return ProjectFiles(product.configured().control_db.parent,
                            total_bytes=policy.artifact_bytes, workspace=workspace_handle(request))

    @app.get(prefix + "/directories")
    def list_strategy_directory(
        request: Request, path: str = Query(default="", max_length=1024),
    ) -> StrategyDirectory:
        return files(request, query=True).list(path)

    @app.post(prefix + "/directories")
    def create_strategy_directory(body: StrategyPath, request: Request) -> StrategyPath:
        return files(request).mkdir(body.path)

    @app.get(prefix + "/files")
    def read_strategy_file(
        request: Request, path: str = Query(min_length=1, max_length=1024),
    ) -> StrategySource:
        return files(request, query=True).read(path)

    @app.put(prefix + "/files")
    def save_strategy_file(body: StrategySave, request: Request) -> StrategyFilePath:
        return files(request).save(body.path, body.source, overwrite=body.overwrite)

    @app.get(prefix + "/projects")
    def read_strategy_project(
        request: Request, path: str | None = Query(default=None, min_length=1, max_length=1024),
        strategy_id: str | None = Query(default=None, pattern=PROJECT_ID_PATTERN),
    ) -> ProjectSource:
        manager = projects(request, query=True)
        if (path is None) == (strategy_id is None):
            raise DomainError("STRATEGY_QUERY_UNSUPPORTED", "Specify exactly one of path or strategy_id", 422)
        return manager.read(path) if path is not None else manager.read_id(strategy_id)

    @app.post(prefix + "/projects")
    def create_strategy_project(body: ProjectCreate, request: Request) -> ProjectSource:
        return projects(request).create(body.path, body.entrypoint)

    @app.put(prefix + "/projects")
    def save_strategy_project(body: ProjectSave, request: Request) -> ProjectSource:
        return projects(request).save(**body.model_dump())

    @app.post(prefix + "/projects/move")
    def move_strategy_project(body: ProjectMove, request: Request) -> ProjectSource:
        return projects(request).move(**body.model_dump())
