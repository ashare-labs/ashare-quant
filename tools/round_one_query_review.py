"""Audit original query-only scope and derive metrics from sealed AQ responses.

This is comparison evidence, not an AD data validator or an independent data source.
No reference values are passed to the metric computations.
"""

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ashare_quant.round_one.query_compare import compare

IDS = ("B21", "B25", "B27", "B28", "C01", "C02", "C03", "C04", "C05")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finite(value):
    return not isinstance(value, dict) and value is not None and Decimal(str(value)).is_finite()


def metrics(cid, events):
    returns = [e for e in events if e["kind"] == "query_return"]
    values = [e["value"] for e in returns]
    if cid == "B21":
        return dict(
            returned_sessions=[[r["date"] for r in v["rows"]] for v in values],
            native_holiday_callbacks_executed=False,
            previous_date_not_computed=True,
        )
    if cid == "B25":
        by = {(v["observed_at"][:10], v["parameters"]["fq"]): v for v in values}
        days = sorted({d for d, _ in by})
        raw = [by[(d, None)]["rows"] for d in days]
        pre = [by[(d, "pre")]["rows"] for d in days]
        post = [by[(d, "post")]["rows"] for d in days]
        return dict(
            raw_rows_unchanged=raw[0] == raw[1],
            post_rows_unchanged=post[0] == post[1],
            pre_changed_fields=sorted(
                {k for a, b in zip(pre[0], pre[1]) for k in a if a[k] != b[k]}
            ),
            anchors=days,
            june13_close_by_anchor={
                d: {
                    str(fq): next(
                        r["close"]
                        for r in by[(d, fq)]["rows"]
                        if r["index"].startswith("2024-06-13")
                    )
                    for fq in (None, "pre", "post")
                }
                for d in days
            },
            independent_adjustment_calculation=False,
        )
    if cid == "B27":
        rows = []
        for v in values:
            for r in v["rows"]:
                volume, money = Decimal(r["volume"]), Decimal(r["money"])
                ratio = money / volume
                rows.append(
                    dict(
                        at=r["index"],
                        frequency=v["parameters"]["frequency"],
                        volume=r["volume"],
                        money=r["money"],
                        money_div_volume=str(ratio),
                        nonzero=volume > 0 and money > 0,
                        within_recorded_high_low=Decimal(r["low"]) <= ratio <= Decimal(r["high"]),
                        reported_avg=r["avg"],
                        avg_minus_computed_ratio=str(Decimal(r["avg"]) - ratio),
                    )
                )
        return dict(
            rows=rows, independent_unit_certification=False, full_day_minute_aggregation=False
        )
    if cid == "B28":
        by = {e["label"]: e["value"]["rows"] for e in returns}
        nan = by["nan_fill"][0]
        return dict(
            filled=by["filled"],
            nan_fields=sorted(
                k for k, v in nan.items() if isinstance(v, dict) and v.get("nonfinite") == "nan"
            ),
            skipped_rows=len(by["skipped"]),
            new_fill_algorithm=False,
        )
    if cid == "C01":
        rows = values[0]["rows"]
        day = values[0]["parameters"]["date"]
        codes = [r["code"] for r in rows]
        return dict(
            rows=len(rows),
            unique_codes=len(set(codes)),
            exchange_counts=dict(Counter(c.split(".")[-1] for c in codes)),
            invalid_observed_date_bounds=[
                r["code"] for r in rows if not r["start_date"] <= day <= r["end_date"]
            ],
            concrete_future_end_dates=sum(day < r["end_date"] < "2200-01-01" for r in rows),
            placeholder_end_dates=sum(r["end_date"] == "2200-01-01" for r in rows),
            independent_universe_difference=None,
        )
    if cid == "C02":
        rows = []
        for e in events:
            if e["kind"] == "query_return":
                field = e["label"]
                value = e["value"]["rows"][0][field]
                rows.append(
                    dict(
                        field=field, status="finite" if finite(value) else "nonfinite", value=value
                    )
                )
            elif e["kind"] == "query_exception":
                rows.append(
                    dict(field=e["label"], status="refused_by_running_AD_API", error=e["error"])
                )
        return dict(
            fields=rows,
            counts=dict(Counter(r["status"] for r in rows)),
            acceptance_is_not_market_meaning=True,
        )
    if cid == "C03":
        info = next(v for v in values if v["api"] == "get_security_info")["rows"][0]
        bars = next(v for v in values if v["api"] == "get_price")["rows"]
        earliest = {
            f: min(r["index"][:10] for r in bars if finite(r[f]))
            for f in ["close", "volume", "money"]
        }
        return dict(
            rows=len(bars),
            observed_listing_date=info["start_date"],
            earliest_nonempty_by_field=earliest,
            independent_history_start=None,
            no_claim_outside_query_window=True,
        )
    if cid == "C04":
        values = sorted(values, key=lambda v: v["parameters"]["date"])
        left, right = ({r["code"] for r in v["rows"]} for v in values)
        return dict(
            dates=[v["parameters"]["date"] for v in values],
            counts=[len(v["rows"]) for v in values],
            unique_counts=[len(left), len(right)],
            intersection=len(left & right),
            old_only=sorted(left - right),
            new_only=sorted(right - left),
            independent_AQ_JQ_set_difference=None,
            metric_scope="within the same observed provider across dates",
        )
    calendar = next(v for v in values if v["api"] == "get_trade_days")
    price = next(v for v in values if v["api"] == "get_price")
    last_session = max(r["date"] for r in calendar["rows"])
    last_bar = max(r["index"][:10] for r in price["rows"])
    capture = price["physical_capture_interval"]
    return dict(
        rows=len(price["rows"]),
        last_calendar_session=last_session,
        last_bar_date=last_bar,
        last_dates_equal=last_session == last_bar,
        logical_clock=price["observed_at"],
        physical_capture_utc={
            k: datetime.fromisoformat(v).astimezone(timezone.utc).isoformat()
            for k, v in capture.items()
        },
        published_at=None,
        revision_id=None,
        second_version=None,
        publication_latency=None,
    )


SCOPES = {
    "B21": (
        "query_and_native_calendar",
        "PARTIAL_NATIVE_CALENDAR_NOT_EXECUTED",
        "固定两次日历查询保真；尚未运行原题节假日调度、previous_date 和空账户回调。",
        "缺执行初始化时可用的该清明窗口及外端点日历绑定；现有04-03 10:00查询不能倒灌到初始化。",
    ),
    "B25": (
        "query_semantics",
        "EXACT_QUERY_PATH_COMPLETED_INDEPENDENT_ADJUSTMENT_UNVERIFIED",
        "两日六份raw/pre/post查询及基准日隔离已完成；本次另外计算跨时钟行和字段变化。",
        "AD返回已观察的复权快照，未由独立raw+行动/因子重新计算；缺独立复权基准和舍入规则，不能称复权引擎对齐。",
    ),
    "B27": (
        "query_semantics",
        "EXACT_QUERY_AND_DIMENSIONAL_CALCULATION_COMPLETED",
        "日线与三分钟查询完成；本次实际用Decimal计算money/volume及价格区间关系，保留avg差值。",
        "结果仍依赖同源观察；没有独立量额单位认证或完整分钟汇总，不以close乘volume造金额。",
    ),
    "B28": (
        "query_semantics",
        "EXACT_VARIANT_QUERY_PRESERVATION_COMPLETED",
        "三种完整参数读取已区分填充值、NaN及空集，未把NaN当0或空结果当无事件。",
        "不是从独立原始停牌行情运行填充/过滤算法；只是AD保存的三种JQ结果。通用填充行为证据不足。",
    ),
    "C01": (
        "query_coverage",
        "OBSERVED_UNIVERSE_QUERY_AND_SET_AUDIT_COMPLETED",
        "全5108行查询和唯一性/范围统计已完成，未来末日仅允许audit角色。",
        "没有独立同日在市证券集合，AQ/JQ集合差集不可计算；未来退市日期和2200哨兵不是历史PIT事实。",
    ),
    "C02": (
        "query_coverage",
        "FROZEN_FIELD_PROBES_AND_LIVE_GUARD_COMPLETED",
        "14字段已逐项读取；12数值、1NaN、1当前AD校验拒绝；没有重放JQ异常。",
        "拒绝类别相应但异常类型/文本不同；同源返回不认证独立字段值或股票open_interest语义。",
    ),
    "C03": (
        "query_coverage",
        "OBSERVED_WINDOW_QUERY_AND_FIRST_DATE_COMPUTATION_COMPLETED",
        "上市资料和固定窗口8行查询已完成；本次从AQ结果实际计算逐字段首个非空日。",
        "缺独立历史覆盖产品；2005-01-04只为本查询最早返回，不证明2000年前后绝对无数据。",
    ),
    "C04": (
        "query_coverage",
        "OBSERVED_MEMBERSHIP_QUERIES_AND_TEMPORAL_DIFF_COMPLETED",
        "两历史日期名单和集合增减计算已完成；105增/105减属于同一来源的时间变化。",
        "没有独立按日指数成员产品；不可将同源名单回读当作AQ/JQ真实差集为0。",
    ),
    "C05": (
        "query_freshness",
        "ONE_OBSERVED_FRESHNESS_SNAPSHOT_COMPLETED_WITH_LIMITS",
        "最近10条和日历查询完成；本次比较最大日期并转换真实采集UTC，逻辑钟单列。",
        "首次发布时间、revision_id、第二版本均缺，不能验证发布延迟、更新或补订修订。",
    ),
}


def audit(root):
    docs = root / "docs/round-one"
    bindings = json.loads((docs / "input-bindings.json").read_text())
    by = {c["case_id"]: c for c in bindings["cases"]}
    results = []
    for cid in IDS:
        path = root / f".verification/round-one/query-runs/{cid}-attempt1"
        fidelity = compare(by[cid], path)
        events = list(map(json.loads, (path / "events.jsonl").read_text().splitlines()))
        computed = metrics(cid, events)
        scope, status, done, gap = SCOPES[cid]
        results.append(
            dict(
                case_id=cid,
                original_primary_scope=scope,
                status=status,
                native_engine_needed_for_primary_query=cid == "B21",
                required_exact_query_path_executed=True,
                observed_snapshot_preservation=dict(
                    fields=fidelity["fields"],
                    status=fidelity["status"],
                    unresolved=fidelity["unresolved_count"],
                    evidence_class="SAME_SOURCE_ROUND_TRIP_NOT_INDEPENDENT_ALIGNMENT",
                ),
                computations_from_sealed_AQ_results=computed,
                completed_limited_capability=done,
                evidence_insufficient=gap,
                full_independent_data_alignment=False,
                still_different=[
                    "AD QueryRefused/JQ_QUERY_FIELD is not JQ AssertionError identity or text"
                ]
                if cid == "C02"
                else [],
                fixed_reporting=[
                    "Same-source snapshot preservation separated from independent value/algorithm validation"
                ],
                frozen_reference_files=by[cid]["files"],
                aq_query_run=str(path),
                aq_complete_sha256=sha(path / "complete.json"),
                original_query_comparison_sha256=sha(docs / f"query-results/{cid}.json"),
            )
        )
    return dict(
        schema="aq.round-one.query-scope-review.v1",
        no_blanket_pass=True,
        new_JQ_runs=0,
        new_native_runs=0,
        query_snapshots_reused=True,
        new_local_metric_computation=True,
        implementation_boundary="AQ research audit only; AD continues to own data acquisition, quality and transformations",
        cases=results,
    )


if __name__ == "__main__":
    print(json.dumps(audit(Path(sys.argv[1]).resolve()), ensure_ascii=False, indent=2))
