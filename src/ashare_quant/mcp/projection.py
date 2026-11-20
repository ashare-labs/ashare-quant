"""Stable bounded pages, path sanitization, and unified run/result projections."""

import base64
import hashlib
import json
import re

from ashare_quant.mcp.client import ResearchError

OMITTED_METADATA_KEYS = {
    "root",
    "data_root",
    "path",
    "deployment",
    "control_db",
    "job_directory",
    "example",
    "assets",
    "owner_descriptor",
    "limitations",
}

HOST_PATH_RE = re.compile(
    r"(?:/private/var/folders|/var/folders|/Users|/home|/tmp)[^\s\"',)]*"
)


def sanitize_text(text: str) -> str:
    def _replace(match: re.Match) -> str:
        raw = match.group(0)
        if raw.endswith("/strategy.py"):
            return "strategy.py"
        if "/source/" in raw:
            return raw.split("/source/", 1)[1]
        return "<local-path>"

    return HOST_PATH_RE.sub(_replace, text)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def page(rows, identity, limit=50, cursor=None):
    snapshot = digest({"identity": identity, "rows": rows})
    offset = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset = token["offset"]
            if (
                token["snapshot"] != snapshot
                or type(offset) is not int
                or offset < 0
                or offset > len(rows)
            ):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise ResearchError(
                "PAGE_CHANGED",
                "Invalid cursor or source changed; restart this query from the first page",
            ) from exc
    end = offset
    size = 0
    for row in rows[offset : offset + limit]:
        row_size = len(json.dumps(row, ensure_ascii=False).encode())
        if size + row_size > 64 * 1024:
            if end == offset:
                raise ResearchError(
                    "ROW_TOO_LARGE", "One row exceeds 64 KiB; narrow fields or query window"
                )
            break
        size += row_size
        end += 1
    token = base64.urlsafe_b64encode(
        json.dumps(dict(snapshot=snapshot, offset=end)).encode()
    ).decode()
    return dict(
        items=rows[offset:end],
        total=len(rows),
        offset=offset,
        next_cursor=token if end < len(rows) else None,
        snapshot_sha256=snapshot,
    )


def compact(value, depth=0):
    """Bound metadata and omit server filesystem locators."""
    if depth > 8:
        return {"omitted": "nested metadata"}
    if isinstance(value, dict):
        return {
            k: compact(v, depth + 1)
            for k, v in list(value.items())[:80]
            if k not in OMITTED_METADATA_KEYS
        }
    if isinstance(value, list):
        items = [compact(v, depth + 1) for v in value[:30]]
        return (
            items if len(value) <= 30 else {"items": items, "total": len(value), "truncated": True}
        )
    if isinstance(value, str):
        cleaned = sanitize_text(value)
        if len(cleaned) > 2000:
            return cleaned[:2000] + " [truncated]"
        return cleaned
    return value


def clean_traceback(raw: str | None) -> list[str]:
    """Keep only user strategy frames and the final exception line, with sanitized paths."""
    if not raw or not isinstance(raw, str):
        return []
    lines = [line.rstrip() for line in raw.strip().splitlines() if line.strip()]
    user_frames: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith('File "'):
            m = re.search(r'File "([^"]+)", line (\d+), in (\S+)', line)
            code_line = lines[i + 1].strip() if i + 1 < len(lines) and not lines[i + 1].lstrip().startswith('File "') else ""
            if m:
                filepath, lineno, func = m.groups()
                is_internal = any(
                    part in filepath
                    for part in (
                        "ashare_quant/backtests/",
                        "ashare_quant/tasks/",
                        "site-packages/rqalpha",
                        "runpy.py",
                        "<string>",
                    )
                )
                if not is_internal:
                    short_file = sanitize_text(filepath)
                    entry = f"{short_file}:{lineno} in {func}"
                    if code_line:
                        entry += f" -> {code_line}"
                    user_frames.append(entry)
            i += 2
            continue
        i += 1
    if lines and not lines[-1].lstrip().startswith(('File "', "Traceback ")):
        user_frames.append(sanitize_text(lines[-1]))
    return user_frames


PYTHON_SECTIONS = ["trades", "positions", "equity", "orders", "strategy_logs"]
ANALYSIS_SECTIONS = ["analysis_series", "drawdowns", "monthly", "rolling"]
EVALUATION_SECTIONS = [
    "evaluation",
    "evaluation_summary",
    "ic",
    "rank_ic",
    "quantile_membership",
    "quantile_returns",
    "spread",
    "stability",
    "turnover",
    "cost_sensitivity",
]


def _normalize_family(family: str | None, request: dict | None = None) -> str:
    if family == "python":
        freq = (request or {}).get("frequency", "1m")
        return "python_daily" if freq == "1d" else "python_minute"
    return family or "python_minute"


def sections(value, family, *, has_analysis: bool = False, has_error_logs: bool = False):
    norm_family = _normalize_family(family)
    if norm_family in {"python_minute", "python_daily"}:
        if value is not None:
            out = list(PYTHON_SECTIONS)
            if has_analysis:
                out.extend(ANALYSIS_SECTIONS)
            out.append("system_logs")
            return out
        return (["strategy_logs"] if has_error_logs else []) + ["system_logs"]
    if norm_family == "factors":
        if value is not None:
            return (
                ["factor_values"]
                + (EVALUATION_SECTIONS if value.get("evaluation") else [])
                + ["system_logs"]
            )
        return ["system_logs"]
    return ["system_logs"]


def run_view(value: dict, *, family: str | None = None, result: dict | None = None, analysis: dict | None = None):
    request = value.get("request", {})
    norm_family = _normalize_family(family or value.get("family"), request)
    state = value.get("display_status") or value.get("status") or "unknown"
    phase = value.get("phase") or state

    submission = {
        k: request[k]
        for k in (
            "name",
            "idempotency_key",
            "strategy_id",
            "start_date",
            "end_date",
            "securities",
            "frequency",
            "initial_cash",
            "parameters",
            "warmup_sessions",
            "factor_id",
            "definition",
            "source_id",
            "history_start",
            "label_end",
        )
        if k in request and request[k] not in (None, (), [], {})
    }
    if "source" in request and request["source"]:
        submission["source_sha256"] = hashlib.sha256(request["source"].encode()).hexdigest()
    if request.get("project") is not None:
        project = request["project"]
        submission["project"] = {
            "project_id": project["project_id"],
            "entrypoint": project["entrypoint"],
            "file_count": len(project["files"]),
            "source_sha256": digest(project),
        }

    progress_raw = value.get("progress")
    progress = None
    if isinstance(progress_raw, dict):
        progress = {
            k: progress_raw[k]
            for k in (
                "phase",
                "elapsed_seconds",
                "completed_trading_days",
                "total_trading_days",
                "simulation_date",
            )
            if k in progress_raw and progress_raw[k] is not None
        }

    error = value.get("error")
    has_error_logs = bool(isinstance(error, dict) and error.get("strategy_logs"))
    out = {
        "job_id": value["job_id"],
        "family": norm_family,
        "state": state,
        "status": state,
        "phase": phase,
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "submission": submission,
        "sections": sections(
            result,
            norm_family,
            has_analysis=analysis is not None,
            has_error_logs=has_error_logs,
        ),
    }
    if progress:
        out["progress"] = progress
    if value.get("read_only"):
        out["read_only"] = True

    if result is not None:
        out["result_sha256"] = digest(result)
        out["summary"] = result.get("summary")
        if norm_family == "factors":
            out["factor"] = compact(result.get("factor"))
            eval_block = result.get("evaluation") or {}
            if eval_block:
                out["evaluation_summary"] = compact(eval_block.get("summary"))
        elif analysis is not None:
            out["metrics"] = {
                k: m.get("value")
                for k, m in (analysis.get("metrics") or {}).items()
                if isinstance(m, dict)
            }
            if analysis.get("comparison"):
                comp = analysis["comparison"]
                out["benchmark"] = {
                    k: comp.get(k)
                    for k in ("selection", "security", "status", "reason")
                    if k in comp
                }
            if analysis.get("costs"):
                out["costs"] = compact(analysis["costs"])
            if analysis.get("trading"):
                out["trading"] = compact(analysis["trading"])

    if isinstance(error, dict) and error:
        logs = error.get("strategy_logs") or []
        out["failure"] = {
            "code": error.get("code"),
            "cause_code": error.get("cause_code"),
            "file": error.get("source_file"),
            "line": error.get("source_line"),
            "message": sanitize_text(str(error.get("message", ""))),
            "user_traceback": clean_traceback(error.get("traceback")),
            "last_strategy_logs": logs[-50:],
            "hint": "Inspect user_traceback and last_strategy_logs; run check_strategy(source=...) before resubmitting.",
        }
    return out


job_view = run_view


def leaves(value, pointer=""):
    """Lossless JSON projection including empty containers; no numeric transformations."""
    if isinstance(value, (dict, list)) and value:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in items:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from leaves(item, pointer + "/" + escaped)
    else:
        yield {"pointer": pointer, "value": value}


def result_rows(
    value,
    family,
    section,
    *,
    analysis: dict | None = None,
    error: dict | None = None,
    system_logs: list | None = None,
):
    norm_family = _normalize_family(family)
    if section == "system_logs" and system_logs is not None:
        return [compact(item) for item in system_logs]
    if section == "strategy_logs" and value is None and isinstance(error, dict):
        if "strategy_logs" in error:
            return list(error.get("strategy_logs") or [])
    if value is None:
        raise ResearchError(
            "M2_JOB_RESULT_NOT_READY",
            "Run has no published result for this section",
            hint="If the run failed, inspect get_run() failure diagnostics or request section='strategy_logs' / 'system_logs'.",
        )
    available = sections(
        value,
        norm_family,
        has_analysis=analysis is not None,
        has_error_logs=bool(isinstance(error, dict) and error.get("strategy_logs")),
    )
    if section not in available:
        raise ResearchError(
            "RESULT_SECTION_UNAVAILABLE",
            f"Section '{section}' is unavailable for this run (available: {', '.join(available)})",
        )
    if norm_family == "factors":
        if section == "factor_values":
            return value["rows"]
        evaluation = value["evaluation"]
        if section == "evaluation":
            return list(leaves(evaluation))
        return evaluation["summary" if section == "evaluation_summary" else section]
    if norm_family in {"python_minute", "python_daily"}:
        if section in ANALYSIS_SECTIONS and analysis is not None:
            key = "series" if section == "analysis_series" else section
            return analysis.get(key) or []
        native = value["native"]
        if section == "equity":
            return [
                {k: v for k, v in row.items() if k != "positions"} for row in native["accounts"]
            ]
        if section == "positions":
            return [
                {"at": row["at"], "security": sid, **position}
                for row in native["accounts"]
                for sid, position in row.get("positions", {}).items()
            ]
        if section == "strategy_logs":
            return list(value["logs"])
        return native[section]
    raise ResearchError(
        "RESULT_SECTION_UNAVAILABLE",
        "This family supports summary and system_logs only; use backend API for legacy details",
    )
