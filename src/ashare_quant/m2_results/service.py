"""Fail-closed sealed JSON projection; GET performs no owner queries or engine work."""

import hashlib
import json
from decimal import Decimal
from importlib.resources import files

from pydantic import ValidationError

from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_sha256
from ashare_quant.m2.common import digest

from .contracts import (
    M2FrozenResult,
    M2ResultDetail,
    M2ResultIndex,
    M2ResultList,
    M2ServingIdentity,
)

CATALOG_SHA256 = "62b0b78cf1a80c1568d19ed9649dc21303558e98c49110f6e1d58a5e94bf60f6"
EXECUTION_COMMIT = "fa91a754efce439450568d1869d30d0929e62a2d"
EXECUTION_TREE = "130a42fb8771b1a90b64ee3a8717dee0c40bd1e8"
EXECUTION_SOURCE_SHA256 = "5e42b025b53bf9e6860d97cac559f862bbe6167d11920f3c03ef3632b42a3141"
EXECUTION_WHEEL_SHA256 = "9fa1a114a7843f5f1f9df576e55dd4270a9da71763a133bf4c44f268f0420e21"
REVIEW_SEAL_SHA256 = "2276eb4176387188de943dab2213cfe4ffaf634b713de307b224ff9a09a45bcc"
RESULT_IDS = {
    "m2a": "m2-m2a-600000-20200103-20200106-fa91a75-v1",
    "w1": "m2-w1-600000-20200103-20200116-fa91a75-v1",
    "w2": "m2-w2-600000-20200106-20200117-fa91a75-v1",
}
SNAPSHOT_SHA256 = {
    "m2a": "c0b5539a445dde62b28c87e8ed0922dc69376cf796dd2f4a9b8de808940dbf7f",
    "w1": "ead7098de436724392dd1d84a366b0e493d34ab8bad914594ae3a0a059afe481",
    "w2": "743a844800feee60743d064a75cf14685088bef2902cf0280c31816ef9e6cf24",
}
SERVING_SOURCE_SHA256 = source_sha256()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError("Sealed M2 result identity or provenance differs")


def _bundle_root():
    return files("ashare_quant").joinpath("result_evidence/m2-fa91a75-v1")


def _verified_bundle():
    root, checking = _bundle_root(), "catalog"
    try:
        _require(not root.is_symlink())
        catalog_path = root.joinpath("catalog.json")
        _require(not catalog_path.is_symlink())
        raw = catalog_path.read_bytes()
        _require(_sha(raw) == CATALOG_SHA256)
        catalog = json.loads(raw)
        _require(
            catalog["schema"] == "m2.results.catalog.v1"
            and catalog["execution_commit"] == EXECUTION_COMMIT
            and catalog["execution_tree"] == EXECUTION_TREE
            and catalog["windows"] == list(RESULT_IDS)
        )
        expected = {"catalog.json", *[e["file"] for e in catalog["entries"]]}
        actual = {p.name for p in root.iterdir()}
        # Missing files retain the useful 503 class; foreign files or directories are conflicts.
        _require(not actual - expected)
        captured, index = {}, {}
        for entry in catalog["entries"]:
            eid, name = entry["evidence_id"], entry["file"]
            checking = eid
            _require(
                name == eid + ".evidence"
                and "/" not in name
                and "\\" not in name
                and ".." not in name
                and eid not in captured
            )
            path = root.joinpath(name)
            _require(not path.is_symlink())
            raw = path.read_bytes()
            _require(len(raw) == entry["size"] and _sha(raw) == entry["sha256"])
            captured[eid], index[eid] = raw, entry
        _require(len(expected) == len(captured) + 1)
        return catalog, captured, index
    except OSError:
        raise DomainError(
            "M2_RESULT_EVIDENCE_UNAVAILABLE",
            "Required sealed M2 evidence is unavailable",
            503,
            details={"evidence_id": checking},
        ) from None
    except (ValueError, KeyError, TypeError):
        raise DomainError(
            "M2_RESULT_EVIDENCE_CONFLICT",
            "Sealed M2 evidence failed identity or directory checks",
            409,
            details={"evidence_id": checking},
        ) from None


def _money(value):
    value = format(Decimal(str(value)), "f")
    return (value.rstrip("0").rstrip(".") if "." in value else value) or "0"


def _account(value):
    return dict(
        cash=value["cash"],
        equity=value["equity"],
        quantity=value["quantity"],
        sellable=value["sellable"],
        average_price=value["average"],
        dividend_receivable=value["receivable"],
        native_daily_transaction_cost=value["cost"],
    )


def _documents(packet):
    _require(set(packet) == {"query", "result", "sources", "evidence", "authorization"})
    return {k: json.loads(v) for k, v in packet.items()}


def _project(catalog, raw, index, window):
    def obj(eid):
        return json.loads(raw[eid])

    review = obj("review.summary")
    author = obj("author.summary")
    proof = obj("projection.proof")
    _require(
        review["status"] == "PASS_BOUNDED_NATIVE_RESEARCH"
        and review["confirmed_blocking_product_bugs"] == 0
    )
    candidate = review["candidate"]
    _require(
        (
            candidate["commit"],
            candidate["tree"],
            candidate["backend_source_sha256"],
            candidate["wheel_sha256"],
            candidate["version"],
        )
        == (
            EXECUTION_COMMIT,
            EXECUTION_TREE,
            EXECUTION_SOURCE_SHA256,
            EXECUTION_WHEEL_SHA256,
            "0.4.0.dev11",
        )
    )
    _require(_sha(raw["review.checksums"]) == catalog["review_seal_sha256"] == REVIEW_SEAL_SHA256)
    checks = dict(
        line.split("  ", 1)[::-1] for line in raw["review.checksums"].decode().splitlines()
    )
    for eid, entry in index.items():
        origin = entry["origin"]
        if origin["kind"] == "independent_review":
            _require(
                checks[origin["path"]] == entry["sha256"]
                and origin["seal_sha256"] == REVIEW_SEAL_SHA256
            )
        elif origin["kind"] in ("git", "git_zip"):
            _require(origin["commit"] == EXECUTION_COMMIT)
            if origin["kind"] == "git_zip":
                _require(origin["archive_sha256"] == catalog["author_archive_sha256"])
    checked = obj("review.author-evidence")["source-native-evidence.zip"]
    _require(
        checked["sha256"] == catalog["author_archive_sha256"]
        and checked["inner_files_verified"] == 145
    )
    native_review = review["native"]
    _require(
        native_review["actual_previous_close_AD08_used_by_strategy"]
        and native_review["source_wheel_economic_equal"]
        and native_review["recovered_native_states_and_extras_equal"]
        and native_review["general_execution_permission"] is False
    )
    _require(
        author["wheel_sha256"] == EXECUTION_WHEEL_SHA256
        and author["production_submission"] is False
        and author["paper"] is False
    )
    base, manifest, completion = (
        obj(window + "." + x) for x in ("receipt", "manifest", "completion")
    )
    journal = base["journal"]
    state = journal["m2_state"]
    session = state["session"]
    request = session["request"]
    _require(
        journal["m2_sha256"] == digest(state)
        and state["complete"]
        and session == manifest["session"]
    )
    _require(
        base["session_identity"] == digest(session)
        and session["entry"]["owner_binding"]["owner_window_id"] == window
    )
    _require(manifest["source_inventory"] == author["backend_runtime_sources"])
    _require(
        _sha(json.dumps(manifest["source_inventory"], sort_keys=True).encode())
        == EXECUTION_SOURCE_SHA256
    )
    _require(
        digest(
            dict(
                schema="m2.native.experiment.v1",
                session=base["session_identity"],
                rq=manifest["rq"],
                runtime=manifest["runtime"],
                backend_sources=manifest["source_inventory"],
                records=manifest["records"],
            )
        )
        == base["identity"]
        == manifest["identity"]
    )
    _require(
        completion["status"] == "COMPLETED"
        and completion["native_run"] is True
        and completion["identity"] == base["identity"]
        and completion["receipt_sha256"] == digest(base)
        and obj(window + ".teardown") == dict(host_closed=True, native_singleton_cleared=True)
    )
    _require(
        base["general_execution_permission"] is False
        and base["paper_admitted"] is False
        and base["historical_pit_verified"] is False
    )
    for n in (1, 2):
        c = obj(window + ".reentry-" + str(n))
        _require(
            c["status"] == "COMPLETED_RECEIPT_REENTRY"
            and c["native_run"] is False
            and c["checkpoint_unchanged"] is True
            and c["identity"] == base["identity"]
            and c["receipt_sha256"] == digest(base)
        )
    header, payload = raw[window + ".checkpoint"].split(b"\n", 1)
    _require(json.loads(header) == dict(schema=1, identity=base["identity"], sha256=_sha(payload)))
    attestation = next(x for x in proof["checks"] if x["window"] == window)
    _require(
        proof["status"] == "PASS"
        and proof["source_commit"] == EXECUTION_COMMIT
        and proof["new_engine_runs"] == 0
        and attestation["checkpoint_sha256"] == index[window + ".checkpoint"]["sha256"]
        and attestation["receipt_sha256"] == index[window + ".receipt"]["sha256"]
        and attestation["receipt_canonical_sha256"] == digest(base)
        and attestation["session_sha256"] == digest(session)
        and attestation["complete"]
        and attestation["receipt_equals_checkpoint"]
        and attestation["projection_equals_committed_state"]
    )
    # HTTP checks the header/hash and independently sealed offline proof; never decodes pickle.
    binding = session["entry"]["binding"]
    bound = session["entry"]["owner_binding"]
    envelope = json.loads(bound["envelope"])
    owner_review = obj("owner.review")
    owner_release = obj("owner.release")
    descriptor = json.loads(bound["descriptor"])
    _require(
        binding["independent_review_sha256"]
        == _sha(raw["owner.review"])
        == review["owner"]["independent_owner_review_sha256"]
    )
    _require(
        bound["release_sha256"]
        == _sha(raw["owner.release"])
        == owner_review["release_receipt_sha256"]
    )
    _require(
        binding["owner_commit"] == owner_release["commit"] == review["owner"]["owner_commit"]
        and binding["owner_wheel_sha256"]
        == owner_release["wheel"]["sha256"]
        == review["owner"]["owner_wheel_sha256"]
        and binding["dataset_id"] == descriptor["dataset_id"] == review["dataset_id"]
        and binding["use_profile_sha256"] == descriptor["profile_sha256"]
        and envelope["owner"] == descriptor
    )
    _require(
        envelope["external_product_review"] == "PENDING"
        and envelope["backend_execution_authorized"] is False
    )
    quality = envelope["original_quality"]
    _require(
        quality["available_at"] is None
        and quality["finality"] == "UNVERIFIED"
        and quality["historical_pit_verified"] is False
        and quality["source_authenticity_verified"] is False
        and quality["verified_absent"] is False
    )
    strategy = session["strategy"]
    definition = strategy["definition"]
    _require(
        definition
        == dict(
            template="previous_close_band.v1",
            target_shares=100,
            enter_below="12.5",
            exit_above="12.55",
        )
    )
    _require(
        strategy["parameter_sha256"] == digest(definition)
        and request["strategy_version_id"] == strategy["version_id"]
        and request["initial_cash"] == "10000"
        and request["initial_positions"] == "empty"
        and request["visibility"] == "assumed"
    )
    dates = [d["trading_date"] for d in session["entry"]["windows"][0]["days"]]
    expected_days = {"m2a": 2, "w1": 10, "w2": 10}[window]
    _require(
        len(dates)
        == len(state["decisions"])
        == len(state["executions"])
        == len(state["day_closes"])
        == expected_days
        and attestation["cursor"] == 3 * expected_days
    )
    obs = base["native_observations"]
    history = [x for x in obs if x["kind"] == "native_history_bars"]
    cache = [
        x for x in obs if x["kind"] == "native_scalar_consumption" and x["details"]["cache_hit"]
    ]
    _require(len(history) == len(cache) == expected_days)
    accounts = {(x["at"][:10], x["phase"]): x for x in base["native_accounts"]}
    fills = []
    for t in base["native_trades"]:
        fills.append(
            dict(
                fill_id=t["trade_id"],
                order_id=t["order_id"],
                side=t["side"],
                quantity=t["quantity"],
                price=t["price"],
                commission=t["commission"],
                tax=t["tax"],
                fee=_money(Decimal(t["commission"]) + Decimal(t["tax"])),
                filled_at=t["at"],
                evidence_id=window + ".receipt",
            )
        )
    _require(
        len({t["fill_id"] for t in fills}) == len(fills)
        and all(t["order_id"] in base["native_orders"] for t in fills)
    )
    engine_reads = [_documents(r["documents"]) for r in base["owner_reads"]]
    _require(not any(d["query"]["operation"] == "valuation_bar" for d in engine_reads))
    day_rows = []
    cumulative = Decimal(0)
    for number, (d, x, c) in enumerate(
        zip(state["decisions"], state["executions"], state["day_closes"], strict=True)
    ):
        t = dates[number]
        p = d["context"]["previous_open"]
        read = d["read_evidence"]
        docs = _documents(read["owner_documents"])
        h = [o for o in history if o["at"][:10] == t]
        consumed = [o for o in cache if o["at"][:10] == t]
        _require(
            len(h) == len(consumed) == 1
            and h[0]["phase"] == "DECIDE"
            and h[0]["at"] == d["context"]["logical_at"] == t + "T09:00:00+08:00"
        )
        h = h[0]["details"]
        consumed = consumed[0]["details"]
        _require(
            h["native_caller"] == "_get_prev_close"
            and h["shape"] == [1]
            and h["dtype"] == "float64"
            and h["call"] == docs["query"]["call"]
            and h["call"]["history_dt"] == p
            and p < t
            and h["call"]["trade_date"] == t
        )
        _require(
            docs["result"]["value"] == read["native_value"]
            and docs["evidence"]["source_evidence_kind"] == "source_claim_unverified"
            and docs["evidence"]["window_id"] == window
            and docs["evidence"]["quality"] == quality
        )
        origin = read["origin_fact"]
        origin_docs = _documents(origin["owner_documents"])
        _require(
            h["documents"] == origin["owner_documents"]
            and all(docs[k] == origin_docs[k] for k in ("query", "result", "sources", "evidence"))
        )
        _require(
            read["cache_hit"]
            and read["native_call_id"] == origin["native_call_id"] == consumed["call_id"]
            and consumed["actual_history_calls"] == 0
            and consumed["authorization"] == _sha(read["owner_documents"]["authorization"].encode())
        )
        _require(json.loads(read["codec_observation"])["native_call_observed"] is False)
        _require(
            x["decision_id"] == c["decision_id"] == d["decision_id"]
            and x["execution_at"] == t + "T15:00:00+08:00"
            and d["cursor_after"] == number * 3 + 1
            and x["cursor_after"] == number * 3 + 2
            and c["cursor_after"] == number * 3 + 3
        )
        phase_account = accounts[(t, "AFTER_TRADING")]
        _require(
            all(c["account"][key] == phase_account[key] for key in ("cash", "quantity", "sellable"))
        )
        day_fills = [v for v in fills if v["filled_at"][:10] == t]
        price_reads = [
            v
            for v in engine_reads
            if v["query"]["operation"] == "execution_bar" and v["query"]["current_date"] == t
        ]
        _require(len(price_reads) <= 1 and (bool(price_reads) or not x["orders"]))
        fee = sum((Decimal(v["fee"]) for v in day_fills), Decimal(0))
        cumulative += fee
        day_rows.append(
            dict(
                trading_date=t,
                previous_open=p,
                decision_at=d["context"]["logical_at"],
                decision_id=d["decision_id"],
                economic_input_sha256=d["economic_input_sha256"],
                signal=d["signal"],
                prior_committed_account=d["account"],
                ad08=dict(
                    native_call_id=read["native_call_id"],
                    native_history_caller=h["native_caller"],
                    native_array_dtype=h["dtype"],
                    native_array_shape=h["shape"],
                    history_call=h["call"],
                    argument_types=h["argument_types"],
                    previous_close=read["native_value"],
                    cache_hit=True,
                    consumption_authorization_sha256=read["consumption_authorization_sha256"],
                    owner_document_sha256={
                        k: _sha(v.encode()) for k, v in read["owner_documents"].items()
                    },
                    owner_documents=docs,
                    first_call=origin,
                    observed_returns=tuple(
                        o["details"]
                        for o in obs
                        if o["kind"] == "native_scalar_consumption" and o["at"][:10] == t
                    ),
                    evidence_id=window + ".receipt",
                ),
                execution_at=x["execution_at"],
                execution_close=price_reads[0]["result"]["close"] if price_reads else None,
                execution_price_read_status="owner_execution_bar_observed"
                if price_reads
                else "not_read_no_order",
                execution_commit_sequence=x["commit_sequence"],
                decision_commit_sequence=d["commit_sequence"],
                day_close_commit_sequence=c["commit_sequence"],
                decision_cursor=d["cursor_after"],
                execution_cursor=x["cursor_after"],
                day_close_cursor=c["cursor_after"],
                no_order_reason=x["no_order_reason"],
                orders=c["final_orders"],
                fills=day_fills,
                account_at_execution=_account(accounts[(t, "SUBMIT_MATCH")]),
                account_at_day_close=_account(phase_account),
                day_close_at=c["account"]["account_as_of"],
                day_fee=_money(fee),
                cumulative_fee=_money(cumulative),
                native_receipt_sha256=x["native_receipt_sha256"],
                evidence_id=window + ".receipt",
            )
        )
    _require(
        _account(base["final_native_account"]) == _account(accounts[(dates[-1], "AFTER_TRADING")])
    )
    independent_audit = next(
        a
        for a in obj("review.economic")["results"]
        if a["path"] == "m2-native-review-evidence/author-evidence/source-native/" + window
    )
    _require(
        independent_audit["final"] == base["final_native_account"]
        and independent_audit["days"] == expected_days
        and independent_audit["native_portfolio_and_atomic_receipt_equal"]
        and Decimal(independent_audit["fees_total"]) == cumulative
    )
    for a, b in zip(independent_audit["daily"], day_rows, strict=True):
        _require(
            a["date"] == b["trading_date"]
            and a["previous_close"] == b["ad08"]["previous_close"]
            and a["execution_close"] == b["execution_close"]
            and Decimal(a["cash"]) == Decimal(b["account_at_day_close"]["cash"])
            and Decimal(a["equity"]) == Decimal(b["account_at_day_close"]["equity"])
        )
    source_recovery = obj("review.source-native")
    wheel_recovery = obj("review.wheel-native")
    _require(
        source_recovery["status"] == wheel_recovery["status"] == "PASS"
        and source_recovery["recovery_points"] == 9
        and wheel_recovery["recovery_points"] == 7
        and wheel_recovery["source_wheel_economic_equal"]
    )
    wheel_faults = {x["fault"] for x in wheel_recovery["recoveries"]}
    cases = [
        dict(
            fault=x["fault"],
            wheel_status="PASS" if x["fault"] in wheel_faults else "NOT_REPEATED",
            native_states_byte_equal=x["native_states_byte_equal"],
            native_extras_equal=x["native_extras_equal"],
            economic_equal=x["economic_equal"],
            crash_snapshots=x["crash_snapshots"],
            evidence_ids=("review.source-native", "review.wheel-native"),
        )
        for x in source_recovery["recoveries"]
    ]
    profile = session["entry"]["profiles"][0]
    assumptions = [
        dict(
            assumption_id=a["assumption_id"],
            owner="ashare-data",
            description={
                "A-EQ": "限定建模域内假定没有相关权益事件；未证明无事件。",
                "A-NORMAL": "限定窗口的普通股挂牌及交易规则投影。",
                "A-AD08": "仅P日raw close乘限定ratio=1，非完整复权能力。",
                "A-VIS": "事后日线条件研究，不证明历史当时已可见。",
            }[a["assumption_id"]],
            sha256=a["content_sha256"],
            version=a["version"],
        )
        for a in envelope["assumption_refs"]
    ]
    assumptions.extend(
        [
            dict(
                assumption_id=k,
                owner="ashare-quant",
                description=text,
                sha256=profile[field],
                version=version,
            )
            for k, text, field, version in (
                (
                    "A-CLOSE",
                    "15:00当日收盘模拟；0滑点，1%成交量上限，原生限价和停牌检查。",
                    "matching_policy_sha256",
                    "m2.A-CLOSE.current_close.conditional.v1",
                ),
                (
                    "A-COST",
                    "固定历史样本：佣金max(5,成交额×0.0008)，卖出税0.001；非现行市场费率声明。",
                    "fee_policy_sha256",
                    "m2.A-COST.rq641.fixed.v1",
                ),
                (
                    "A-POOL",
                    "固定600000普通股、现货多头、T+1、初始零仓。",
                    "pool_policy_sha256",
                    "m2.A-POOL.600000.fixed.v1",
                ),
            )
        ]
    )
    commission = sum((Decimal(f["commission"]) for f in fills), Decimal(0))
    tax = sum((Decimal(f["tax"]) for f in fills), Decimal(0))
    return M2FrozenResult(
        result_id=RESULT_IDS[window],
        evidence_version=catalog["evidence_version"],
        evidence_manifest_sha256=CATALOG_SHA256,
        scope=dict(window_id=window, trading_dates=dates, initial_cash=request["initial_cash"]),
        strategy=strategy,
        summary=dict(
            initial_cash=request["initial_cash"],
            final_cash=base["final_native_account"]["cash"],
            final_equity=base["final_native_account"]["equity"],
            net_change=_money(
                Decimal(base["final_native_account"]["equity"]) - Decimal(request["initial_cash"])
            ),
            final_quantity=base["final_native_account"]["quantity"],
            commission_total=_money(commission),
            tax_total=_money(tax),
            fee_total=_money(commission + tax),
            committed_order_count=len(base["native_orders"]),
            committed_fill_count=len(fills),
            actual_history_call_count=len(history),
            native_cache_consumption_count=len(cache),
        ),
        independent_review=dict(
            status=review["status"],
            reviewed_at=review["reviewed_at_utc"],
            execution_commit=EXECUTION_COMMIT,
            summary_sha256=index["review.summary"]["sha256"],
            report_sha256=index["review.report"]["sha256"],
        ),
        execution_identity=dict(
            code_commit=EXECUTION_COMMIT,
            code_tree=EXECUTION_TREE,
            source_sha256=EXECUTION_SOURCE_SHA256,
            wheel_sha256=EXECUTION_WHEEL_SHA256,
            native_run_identity=base["identity"],
            session_sha256=base["session_identity"],
        ),
        data_identity=dict(
            owner_version=binding["owner_version"],
            owner_commit=binding["owner_commit"],
            owner_wheel_sha256=binding["owner_wheel_sha256"],
            dataset_id=binding["dataset_id"],
            manifest_sha256=binding["manifest_sha256"],
            profile_sha256=binding["use_profile_sha256"],
            query_contract_sha256=binding["query_contract_sha256"],
            data_review_sha256=binding["independent_review_sha256"],
            original_quality=quality,
            owner_descriptor=descriptor,
            source_references=tuple(v["ad08"]["owner_documents"]["sources"] for v in day_rows),
        ),
        assumptions=assumptions,
        assumption_bundle_sha256=request["assumptions"]["profile_sha256"],
        account=_account(base["final_native_account"]),
        days=day_rows,
        recovery=dict(
            selected_window_recovery_exercised=window == "w1",
            cases=cases,
            evidence_ids=(
                "review.summary",
                "review.source-native",
                "review.wheel-native",
                window + ".reentry-1",
                window + ".reentry-2",
            ),
        ),
        evidence=[
            dict(
                evidence_id=e["evidence_id"],
                type=e["type"],
                sha256=e["sha256"],
                size_bytes=e["size"],
                provenance=e["origin"],
            )
            for e in catalog["entries"]
        ],
        limitations=(
            "仅固定dev11执行候选通过独审；当前只读服务修订待单独验收。",
            "三个窗口共11个不同交易日，两个十日窗口高度重叠。",
            "available_at、历史PIT、数据真实性、权益完整性与日线finality等未知项未升级。",
            "09:00决策仅消费P；15:00价格是事后日线撮合模型。",
            "原生daily cost不是累计费用；累计费用仅从已提交成交的commission/tax求和。",
            "无订单日execution_close可为空；没有实际valuation_bar公开查询，估值使用原生已取得的价格缓存。",
            "恢复9/7点是w1测试；各窗口各完成重入2次，不宣称每窗口均做全部恢复。",
            "本样本未覆盖现金不足、实际部分成交、停牌涨跌停拒单和真实公司行动等全部原生分支。",
            "通用执行、严格PIT、分钟、聚宽等价、paper/live、任意策略或不可信checkpoint均未准入。",
        ),
    )


def _all_results():
    if source_sha256() != SERVING_SOURCE_SHA256:
        raise DomainError(
            "M2_RESULT_SERVICE_CHANGED", "Serving code changed; restart with the fixed package", 409
        )
    catalog, raw, index = _verified_bundle()
    try:
        values = {w: _project(catalog, raw, index, w) for w in RESULT_IDS}
        for w, v in values.items():
            _require(digest(v) == SNAPSHOT_SHA256[w])
    except (
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        StopIteration,
        ValidationError,
        ArithmeticError,
    ):
        raise DomainError(
            "M2_RESULT_EVIDENCE_CONFLICT",
            "Sealed result source, review or projection is inconsistent",
            409,
        ) from None
    # No cached success fallback: revalidate the entire directory after projection.
    after_catalog, after_raw, _ = _verified_bundle()
    if after_catalog != catalog or after_raw != raw:
        raise DomainError("M2_RESULT_EVIDENCE_CONFLICT", "Evidence changed while reading", 409)
    if source_sha256() != SERVING_SOURCE_SHA256:
        raise DomainError(
            "M2_RESULT_SERVICE_CHANGED", "Serving code changed during result read", 409
        )
    return values


def _serving():
    return M2ServingIdentity(backend_version=__version__, source_sha256=SERVING_SOURCE_SHA256)


def result_detail(result_id):
    window = next((w for w, rid in RESULT_IDS.items() if rid == result_id), None)
    if window is None:
        raise DomainError("M2_RESULT_NOT_FOUND", "Unknown sealed M2 result", 404)
    result = _all_results()[window]
    return M2ResultDetail(
        **result.model_dump(mode="json"),
        service_identity=_serving(),
        result_snapshot_sha256=digest(result),
    )


def list_results():
    values = _all_results()
    return M2ResultList(
        service_identity=_serving(),
        evidence_manifest_sha256=CATALOG_SHA256,
        items=tuple(
            M2ResultIndex(**{k: getattr(v, k) for k in M2ResultIndex.model_fields})
            for v in values.values()
        ),
    )
