"""Probe implemented contracts and live discovery without exposing hash ceremony."""

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
        and {"source_id", "factor_id", "definition", "history_start"}
        <= set(request.get("required", []))
        and {"environment_id", "evaluation", "parameters", "label_end"}
        <= set(request.get("properties", {}))
        and result.get("schema_version", {}).get("const") == "aq.factor-exploration.v1"
        and {"rows", "evaluation", "provenance"} <= set(result)
    )


def daily_contract(document, catalogue):
    model = schemas(document).get("PythonCreate", {})
    props = model.get("properties", {})
    return (
        model.get("additionalProperties") is False
        and {"trusted_local_code", "accept_research_model"} <= set(props)
        and all(
            props.get(k, {}).get("const") is True
            for k in ("trusted_local_code", "accept_research_model")
        )
        and "1d" in props.get("frequency", {}).get("enum", [])
        and "warmup_sessions" in props
        and "1d" in catalogue.get("frequencies", [])
        and isinstance(catalogue.get("daily"), dict)
        and valid_environment(catalogue.get("environment_id"))
        and catalogue.get("data_status", {}).get("status") != "unavailable"
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
        and source_value.get("environment_id") == value.get("environment_id")
        and isinstance(source_value.get("items"), list)
    )
    py_val = python.get("value", {})
    daily_ready = bool(daily_contract(document, py_val))
    minute_ready = bool(
        python["available"]
        and "/api/v1/python/jobs" in paths
        and valid_environment(py_val.get("environment_id"))
        and py_val.get("data_status", {}).get("status") != "unavailable"
    )
    return dict(
        adapter_version="research-mcp.v2.0",
        backend_openapi_sha256=digest(document) if schema["available"] else None,
        product=config,
        python_minute=python,
        factor_sources=source_value.get("items", []) if factor_ready else [],
        task_families={
            family: {
                "read_only": family in {"portfolio", "daily"},
                "submission_enabled": (
                    factor_ready
                    if family == "factors"
                    else daily_ready
                    if family == "python_daily"
                    else minute_ready
                    if family == "python_minute"
                    else False
                ),
                "available": (
                    factor_ready
                    if family == "factors"
                    else daily_ready
                    if family == "python_daily"
                    else prefix + "/jobs" in paths
                ),
            }
            for family, prefix in PREFIXES.items()
        },
        extensions={
            "python_minute": dict(
                available=minute_ready,
                reason=None
                if minute_ready
                else "Minute Python catalogue and configured market release required",
            ),
            "python_daily": dict(
                available=daily_ready,
                limits=py_val.get("daily") if daily_ready else None,
                reason=None
                if daily_ready
                else "Daily Python schema and live frequency declaration required",
            ),
            "factors": dict(
                discovered_paths=factor_paths,
                available=factor_ready,
                count=len(value.get("items", [])),
                evaluation_available=factor_ready and value.get("evaluation_available") is True,
                limits=value.get("limits"),
                reason=None
                if factor_ready
                else "Compatible factor contract and live catalogue/sources required",
                errors=[x["error"] for x in (catalogue, sources) if not x["available"]],
            ),
        },
        adapter_limits=dict(
            page_size=200,
            daily_securities=8,
            daily_calendar_days=31,
            minute_calendar_days=3,
            initial_cash_max=10_000_000_000,
            backend_response_bytes=32 * 1024 * 1024,
        ),
    )


async def environment_view(backend, *, detail: bool = False):
    raw = await capabilities(backend)
    prod = raw["product"].get("value") or {}
    py = raw["python_minute"].get("value") or {}
    avail = py.get("data_availability") or {}
    minute_sources_resp = await backend.optional("/api/v1/product/minute/sources")
    ms_val = minute_sources_resp.get("value") or {}
    compact_sources = [
        {
            k: s.get(k)
            for k in ("id", "ready", "price_basis", "adjustment", "total_securities")
            if k in s and s.get(k) is not None
        }
        for s in raw.get("factor_sources", [])
    ]
    summary = dict(
        adapter_version=raw["adapter_version"],
        ready=bool(prod.get("configured") and raw["extensions"]["python_minute"]["available"]),
        calendar={
            k: avail.get(k)
            for k in (
                "calendar_start",
                "calendar_end",
                "first_execution_session",
                "last_execution_session",
            )
            if avail.get(k) is not None
        }
        or None,
        frequencies=py.get("frequencies") or ["1m", "1d"],
        default_benchmark=(py.get("benchmark") or {}).get("default_security", "000300.XSHG"),
        minute_sources={
            "default_source": ms_val.get("default_source"),
            "sources": [
                s.get("source") if isinstance(s, dict) else s
                for s in (ms_val.get("sources") or [])
            ],
        }
        if minute_sources_resp["available"]
        else None,
        extensions={
            "python_minute": raw["extensions"]["python_minute"],
            "python_daily": raw["extensions"]["python_daily"],
            "factors": {
                **raw["extensions"]["factors"],
                "sources": compact_sources,
            },
        },
        limits=raw["adapter_limits"],
        resources=[
            "aquant://api-reference",
            "aquant://model-notes",
            "aquant://templates/daily_rebalance",
            "aquant://templates/minute_timing",
            "aquant://templates/multi_file_project",
        ],
    )
    if detail:
        summary["task_families"] = raw["task_families"]
        summary["product"] = compact(raw["product"])
        summary["python_catalogue"] = compact(raw["python_minute"])
        summary["backend_openapi_sha256"] = raw["backend_openapi_sha256"]
    return compact(summary)


async def require_extension(backend, name):
    value = (await capabilities(backend))["extensions"][name]
    if not value["available"]:
        raise ResearchError("CAPABILITY_UNAVAILABLE", value["reason"], capability=name)
    return value
