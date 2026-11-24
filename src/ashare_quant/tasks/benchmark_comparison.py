"""Read-only reference choices from a task's pinned publications, never latest."""

from ashare_data.core.model import DataError
from ashare_data.product.market_release import ReleasedDailyProduct

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import digest
from ashare_quant.tasks.minute.benchmark import empty_observations, published_observations

NAMES = {"000300.XSHG": "沪深300", "000001.XSHG": "上证指数"}


def name(security, supplied=None):
    return NAMES.get(security) or supplied or security


def _referenced_securities(result):
    wanted = set()
    native = result.get("native", {}) if isinstance(result.get("native"), dict) else {}
    for row in native.get("trades") or []:
        if isinstance(row, dict) and isinstance(row.get("security"), str):
            wanted.add(row["security"])
    for account in native.get("accounts") or []:
        if isinstance(account, dict) and isinstance(account.get("positions"), dict):
            wanted.update(k for k in account["positions"] if isinstance(k, str))
    if isinstance(native.get("corporate_entitlements"), dict):
        wanted.update(k for k in native["corporate_entitlements"] if isinstance(k, str))
    return wanted


def release_security_names(release, wanted):
    wanted = [sid for sid in dict.fromkeys(wanted) if isinstance(sid, str) and sid]
    if not wanted:
        return {}
    found = {}
    instruments = getattr(release, "instruments", {})
    for sid in wanted:
        raw = instruments.get(sid, {}).get("name")
        if isinstance(raw, str) and raw.strip() and raw.strip() != sid:
            found[sid] = raw.strip()
    missing = [sid for sid in wanted if sid not in found]
    if missing and hasattr(release, "metadata"):
        import sqlite3

        placeholders = ",".join("?" for _ in missing)
        try:
            with release.metadata() as db:
                rows = db.execute(
                    f"SELECT r.security, i.name FROM release_identifiers r "
                    f"JOIN release_instruments i USING(instrument_id) "
                    f"WHERE r.security IN ({placeholders})",
                    missing,
                ).fetchall()
            for sec_code, sec_name in rows:
                if isinstance(sec_name, str) and sec_name.strip() and sec_name.strip() != sec_code:
                    found[sec_code] = sec_name.strip()
        except sqlite3.Error:
            pass
    return found


def _extract_security_names(owners, result):
    wanted = _referenced_securities(result)
    if not wanted:
        return {}
    found = {}
    for _, owner in owners:
        remaining = [sid for sid in wanted if sid not in found]
        if not remaining:
            break
        found.update(release_security_names(owner.release, remaining))
    return found


def session_security_names(result, session):
    pin = session.get("release") if isinstance(session, dict) else None
    if not pin:
        return {}
    try:
        owner = ReleasedDailyProduct({"market_release": pin})
    except (FileNotFoundError, DataError, KeyError, TypeError):
        return {}
    return _extract_security_names([(pin, owner)], result)


def comparison_and_names(result, session, selection):
    """Return new benchmark evidence, metadata, and pinned security names."""
    original = result.get("native", {}).get("benchmark")
    metadata = {
        "selection": selection, "source": "frozen" if selection == "frozen" else "derived",
        "original_benchmark_sha256": digest(original),
        "original_security": original.get("security") if original else None,
        "options": [{"value": "frozen", "label": "原始基准"}, {"value": "none", "label": "无基准"}],
        "reference_status": "available", "reference_reason": None,
        "release_policy": "task_frozen_publications_only",
    }
    owners = []
    try:
        for pin in (session.get("release"), session.get("benchmark_release")):
            if pin and pin not in [p for p, _ in owners]:
                owners.append((pin, ReleasedDailyProduct({"market_release": pin})))
    except FileNotFoundError:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_SOURCE_UNAVAILABLE")
    except DataError:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_SOURCE_INVALID")
    if not owners and metadata["reference_reason"] is None:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_RELEASE_UNAVAILABLE")
    security_names = _extract_security_names(owners, result)
    choices = {}
    for _, owner in owners:
        for security, instrument in owner.release.instruments.items():
            if instrument.get("asset_class") == "index":
                choices.setdefault(security, {"value": security, "label": name(security, instrument.get("name"))})
    metadata["options"].extend(choices[key] for key in sorted(choices))
    if selection == "frozen":
        return (original, metadata), security_names
    value = empty_observations(None if selection == "none" else selection, "comparison")
    if selection == "none":
        return (value, metadata), security_names
    # Use the first publication (the task's original market) for the preceding
    # session. Taking the previous observed index row would hide a missing base.
    market = next((owner for pin, owner in owners if pin == session.get("release")), None)
    if market is None:
        return ({**value, "reason": metadata["reference_reason"] or "BENCHMARK_CALENDAR_UNAVAILABLE"}, metadata), security_names
    days = result.get("preparation", {}).get("trading_dates")
    if not isinstance(days, list) or not days or any(not isinstance(day, str) for day in days):
        return ({**value, "reason": "BENCHMARK_CALENDAR_UNAVAILABLE"}, metadata), security_names
    try:
        with market.release.metadata() as db:
            prior = db.execute("SELECT date FROM calendar WHERE date < ? ORDER BY date DESC LIMIT 1",
                               [days[0]]).fetchone()
        if prior is None:
            return ({**value, "reason": "BENCHMARK_BASE_MISSING", "missing_dates": []}, metadata), security_names
        value, _ = published_observations(market, selection, days, prior[0],
                                          supplemental_pin=session.get("benchmark_release"))
    except FileNotFoundError:
        value.update(reason="BENCHMARK_SOURCE_UNAVAILABLE")
    except DataError:
        value.update(reason="BENCHMARK_SOURCE_INVALID")
    value["name"] = name(selection, value.get("name"))
    return (value, metadata), security_names


def comparison(result, session, selection):
    """Return new benchmark evidence and metadata; never mutate frozen inputs."""
    selected, _ = comparison_and_names(result, session, selection)
    return selected


def check_query(request, analysis_version, benchmark):
    allowed = {"analysis_version", "benchmark"}
    if (set(request.query_params) - allowed
            or any(len(request.query_params.getlist(key)) > 1 for key in allowed)):
        raise DomainError("RESULT_ANALYSIS_QUERY_UNSUPPORTED", "Use only documented analysis fields", 422)
    if analysis_version == "returns-v1" and benchmark != "frozen":
        raise DomainError("RESULT_ANALYSIS_QUERY_UNSUPPORTED", "Comparison requires returns-v2", 422)
