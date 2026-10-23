"""Directory and source file operations; saving never submits or imports Python code."""

from typing import Literal

from fastapi import Query, Request
from pydantic import Field, field_validator

from ashare_quant.contracts import Contract, DomainError
from ashare_quant.strategies.files import MAX_SOURCE_BYTES, StrategyFiles, parts, source_bytes


class StrategyPath(Contract):
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path", mode="before")
    @classmethod
    def relative_path(cls, value):
        if isinstance(value, str):
            parts(value)
        return value


class StrategySave(StrategyPath):
    source: str = Field(max_length=MAX_SOURCE_BYTES)
    overwrite: bool = Field(default=False, strict=True)

    @field_validator("source", mode="before")
    @classmethod
    def utf8_source(cls, value):
        if isinstance(value, str):
            source_bytes(value)
        return value


class StrategySource(StrategyPath):
    source: str


class StrategyEntry(StrategyPath):
    name: str
    kind: Literal["directory", "file"]


class StrategyDirectory(Contract):
    root: str
    path: str
    entries: list[StrategyEntry]


def mount(app, product):
    prefix = "/api/v1/python"

    def files(request, *, query=False):
        allowed = {"path"} if query else set()
        if (set(request.query_params) - allowed
                or len(request.query_params.getlist("path")) > 1):
            raise DomainError("STRATEGY_QUERY_UNSUPPORTED", "Use only the documented fields", 422)
        return StrategyFiles(product.configured().control_db.parent)

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
    def save_strategy_file(body: StrategySave, request: Request) -> StrategyPath:
        return files(request).save(body.path, body.source, overwrite=body.overwrite)
