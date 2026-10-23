"""Probe implemented contracts and live discovery, never infer support from a route alone."""

import re

from ashare_quant.mcp.client import PREFIXES, ResearchError
from ashare_quant.mcp.projection import compact, digest


def schemas(document):
    return document.get("components", {}).get("schemas", {})


def valid_environment(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def factor_contract(document):
    models = schemas(document)
    request = models.get("FactorCreate", {})
    result = models.get("FactorResult", {}).get("properties", {})
    return (
        request.get("additionalProperties") is False
        and {"environment_id", "source_id", "factor_id", "definition", "history_start"}
        <= set(request.get("required", []))
        and {"evaluation", "parameters", "label_end"} <= set(request.get("properties", {}))
        and result.get("schema_version", {}).get("const") == "aq.factor-exploration.v1"
        and {"rows", "evaluation", "provenance"} <= set(result)
    )


def daily_contract(document, catalogue):
    model = schemas(document).get("PythonCreate", {})
    props = model.get("properties", {})
    return (
        model.get("additionalProperties") is False
        and {"trusted_local_code", "accept_research_model"} <= set(model.get("required", []))
        and all(
            props.get(k, {}).get("const") is True
            for k in ("trusted_local_code", "accept_research_model")
        )
        and "1d" in props.get("frequency", {}).get("enum", [])
        and "warmup_sessions" in props
        and "1d" in catalogue.get("frequencies", [])
        and isinstance(catalogue.get("daily"), dict)
        and valid_environment(catalogue.get("environment_id"))
    )


def catalogue_valid(value):
    return (
        valid_environment(value.get("environment_id"))
        and isinstance(value.get("items"), list)
        and all(
            isinstance(row, dict) and {"id", "definition", "library"} <= set(row)
            for row in value["items"]
        )
    )


async def capabilities(backend):
    config = await backend.optional("/api/v1/product/config")
    python = await backend.optional("/api/v1/python/catalogue")
    schema = await backend.optional("/api/v1/openapi.json")
    document = schema.get("value", {})
    paths = document.get("paths", {})
    factor_paths = {
        "/api/v1/factors/" + part: "/api/v1/factors/" + part in paths
        for part in ("catalogue", "sources", "jobs")
    }
    catalogue = await backend.optional("/api/v1/factors/catalogue")
    sources = await backend.optional("/api/v1/factors/sources")
    value = catalogue.get("value", {})
    source_value = sources.get("value", {})
    factor_ready = bool(
        all(factor_paths.values())
        and factor_contract(document)
        and catalogue_valid(value)
        and source_value.get("environment_id") == value["environment_id"]
        and isinstance(source_value.get("items"), list)
    )
    daily_ready = bool(daily_contract(document, python.get("value", {})))
    return compact(
        dict(
            adapter_version="research-mcp.v1.1",
            backend_openapi_sha256=digest(document) if schema["available"] else None,
            product=config,
            python_minute=python,
            task_families={
                family: {
                    "available": (
                        factor_ready
                        if family == "factors"
                        else daily_ready
                        if family == "python_daily"
                        else prefix + "/jobs" in paths
                    )
                }
                for family, prefix in PREFIXES.items()
            },
            extensions={
                "factors": dict(
                    discovered_paths=factor_paths,
                    available=factor_ready,
                    environment_id=value.get("environment_id") if factor_ready else None,
                    count=len(value.get("items", [])),
                    evaluation_available=factor_ready and value.get("evaluation_available") is True,
                    limits=value.get("limits"),
                    reason=None
                    if factor_ready
                    else "Compatible factor contract and live catalogue/sources required",
                    errors=[x["error"] for x in (catalogue, sources) if not x["available"]],
                ),
                "python_daily": dict(
                    available=daily_ready,
                    environment_id=python.get("value", {}).get("environment_id")
                    if daily_ready
                    else None,
                    limits=python.get("value", {}).get("daily") if daily_ready else None,
                    reason=None
                    if daily_ready
                    else "Daily Python schema and live frequency declaration required",
                ),
            },
            adapter_limits=dict(
                page_size=200,
                daily_securities=8,
                daily_calendar_days=31,
                minute_calendar_days=3,
                backend_response_bytes=32 * 1024 * 1024,
            ),
            trust="Trusted local Python only; process supervision is not a hostile-code sandbox",
        )
    )


async def require_extension(backend, name):
    value = (await capabilities(backend))["extensions"][name]
    if not value["available"]:
        raise ResearchError("CAPABILITY_UNAVAILABLE", value["reason"], capability=name)
    return value


def require_environment(value, expected):
    if value.get("environment_id") != expected:
        raise ResearchError("ENVIRONMENT_CHANGED", "Rediscover the changed research environment")
