"""Stable bounded pages without storing a second copy of backend research state."""

import base64
import hashlib
import json

from ashare_quant.mcp.client import ResearchError


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
                    "ROW_TOO_LARGE", "One row exceeds 64 KiB; use a controlled backend export"
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
    """Bound metadata and omit server filesystem locators, never use them as inputs."""
    if depth > 8:
        return {"omitted": "nested metadata"}
    if isinstance(value, dict):
        return {
            k: compact(v, depth + 1)
            for k, v in list(value.items())[:80]
            if k
            not in {
                "root",
                "path",
                "deployment",
                "control_db",
                "job_directory",
                "example",
                "assets",
                "owner_descriptor",
            }
        }
    if isinstance(value, list):
        items = [compact(v, depth + 1) for v in value[:30]]
        return (
            items if len(value) <= 30 else {"items": items, "total": len(value), "truncated": True}
        )
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + " [truncated]"
    return value


def job_view(value):
    keys = (
        "job_id",
        "contract_version",
        "status",
        "stored_status",
        "display_status",
        "execution_state",
        "execution_reason",
        "phase",
        "created_at",
        "updated_at",
        "attempt_count",
        "committed_cursor",
        "total_cursors",
        "cancellation_requested_at",
        "recovery_required",
        "can_resume",
        "read_only",
        "family",
        "verification",
        "original_service_identity",
        "result",
        "error",
        "manifest_sha256",
        "session_sha256",
    )
    result = compact({k: value[k] for k in keys if k in value})
    request = value.get("request", {})
    result["submission"] = {
        k: request[k]
        for k in (
            "name",
            "idempotency_key",
            "environment_id",
            "start_date",
            "end_date",
            "securities",
            "frequency",
            "warmup_sessions",
            "factor_id",
            "definition",
            "source_id",
            "history_start",
            "label_end",
        )
        if k in request
    }
    if "source" in request:
        result["submission"]["source_sha256"] = hashlib.sha256(
            request["source"].encode()
        ).hexdigest()
    return result


PYTHON_SECTIONS = ["trades", "positions", "equity", "orders", "strategy_logs"]
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


def sections(value, family):
    if family in {"python_minute", "python_daily"}:
        return PYTHON_SECTIONS
    if family == "factors":
        return ["factor_values"] + (EVALUATION_SECTIONS if value.get("evaluation") else [])
    return []


def leaves(value, pointer=""):
    """Lossless JSON projection including empty containers; no numeric transformations."""
    if isinstance(value, (dict, list)) and value:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in items:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from leaves(item, pointer + "/" + escaped)
    else:
        yield {"pointer": pointer, "value": value}


def result_rows(value, family, section):
    if section not in sections(value, family):
        raise ResearchError("RESULT_SECTION_UNAVAILABLE", "Section unavailable for this result")
    if family == "factors":
        if section == "factor_values":
            return value["rows"]
        evaluation = value["evaluation"]
        if section == "evaluation":
            return list(leaves(evaluation))
        return evaluation["summary" if section == "evaluation_summary" else section]
    if family in {"python_minute", "python_daily"}:
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
            return value["logs"]
        return native[section]
    # Other result layouts are reported explicitly, never guessed from engine capacity.
    raise ResearchError(
        "RESULT_SECTION_UNAVAILABLE",
        "This family supports summary only in MCP v1; use its backend result API for details",
    )
