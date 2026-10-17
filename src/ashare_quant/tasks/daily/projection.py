"""Typed dynamic projections from the committed native receipt, never attempt traces."""

import json
from decimal import Decimal

from ashare_quant.storage.canonical import digest
from ashare_quant.tasks.daily.contracts import JobResult
from ashare_quant.tasks.projections import _account, _documents, _money, _require, _sha


def project(manifest, base, proof, evidence, recovery):
    # Existing contract/static APIs must not import the data owner at startup.
    # Its public document codec is needed only for this dynamic projection.
    from ashare_quant.tasks.daily.engine_evidence import historical_price_reads

    session = manifest.session
    window = manifest.request.window_id
    dates = [d.isoformat() for d in manifest.execution_scope.trading_dates]
    expected_days = len(dates)
    quality = manifest.approved_window.data_identity.original_quality
    from ashare_quant.backtests.daily.journal import restore_extension
    restore_extension(base["journal"], expected_session_sha256=manifest.session_sha256)
    state = base["journal"]["m2_state"]
    _require(
        base["journal"]["m2_sha256"] == digest(state)
        and state["complete"]
        and state["session"] == session
        and base["session_identity"] == manifest.session_sha256
        and base["identity"] == proof["native_identity"]
        and base["general_execution_permission"] is False
        and base["paper_admitted"] is False
        and base["historical_pit_verified"] is False
        and base["native_AD08_observed"] is True
    )
    _require(
        len(state["decisions"])
        == len(state["executions"])
        == len(state["day_closes"])
        == expected_days
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
    prices = historical_price_reads(manifest, base)
    for packet in base["owner_reads"]:
        query = _documents(packet["documents"])["query"]
        if query["operation"] in {"execution_bar", "valuation_bar", "trading_state"}:
            _require(
                packet["context"]["phase"] in {"SUBMIT_MATCH", "AFTER_TRADING", "SETTLEMENT"}
                and packet["context"]["consumer"] == "engine"
                and query["target_date"] == query["current_date"]
            )
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
            for v in (read.owner_documents for read in prices)
            if v["query"]["operation"] == "execution_bar" and v["query"]["current_date"] == t
        ]
        # All original/recovery reads have been independently bound to the same
        # stable fact above. Keep each packet; project that fact's price once.
        _require(bool(price_reads) or not x["orders"])
        fee = sum((Decimal(v["fee"]) for v in day_fills), Decimal(0))
        cumulative += fee
        day_rows.append(
            dict(
                trading_date=t,
                decision_verification="trusted_python_record.v1" if d.get("python_receipt") else "pure_rule_replay",
                python_decision=d.get("python_receipt"),
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
                native_reason=x["native_reason"],
                native_rejections=[
                    dict(
                        observed_at=o["at"],
                        native_observation_index=i,
                        generation=o["generation"],
                        cursor=o["cursor"],
                        order_id=o["details"]["order_id"],
                        reason=o["details"]["reason"],
                    )
                    for i, o in enumerate(obs)
                    if o["kind"] == "native_order_event"
                    and o["at"][:10] == t
                    and o["phase"] == "SUBMIT_MATCH"
                    and o["details"]["event"] == "ORDER_CREATION_REJECT"
                ],
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
    commission = sum((Decimal(f["commission"]) for f in fills), Decimal(0))
    tax = sum((Decimal(f["tax"]) for f in fills), Decimal(0))
    return JobResult(
        execution_scope=manifest.execution_scope,
        strategy=manifest.strategy,
        engine_price_reads=prices,
        valuation_bar_query_observed=any(
            d["query"]["operation"] == "valuation_bar" for d in engine_reads
        ),
        job_id=manifest.job_id,
        request=manifest.request,
        scope=manifest.approved_window.scope.model_copy(
            update={"initial_cash": manifest.request.parameters.initial_cash, "trading_dates": manifest.execution_scope.trading_dates}
        ),
        summary=dict(
            initial_cash=manifest.request.parameters.initial_cash,
            final_cash=base["final_native_account"]["cash"],
            final_equity=base["final_native_account"]["equity"],
            net_change=_money(
                Decimal(base["final_native_account"]["equity"])
                - Decimal(manifest.request.parameters.initial_cash)
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
        account=_account(base["final_native_account"]),
        days=day_rows,
        execution_identity=dict(
            backend_version=manifest.service_identity.backend_version,
            source_sha256=manifest.executor_source_sha256,
            runtime_sha256=digest(proof["runtime"]),
            native_run_identity=base["identity"],
            session_sha256=base["session_identity"],
        ),
        service_identity=manifest.service_identity,
        data_identity=manifest.approved_window.data_identity,
        assumptions=manifest.approved_window.assumptions,
        assumption_bundle_sha256=manifest.request.assumption_ack_sha256,
        manifest_sha256=digest(manifest),
        evidence=evidence,
        recovery=recovery,
        limitations=(
            "有限离线条件研究；当前任务与控制层尚未独立验收。",
            "历史PIT、来源真实性及完整性未证；保留A数达原始质量结论与七项条件假设。",
            "固定600000、日频、原owner窗口内可选结束交易日；Python仅CLI注册可信本地单文件无跨日状态；不开放分钟、paper、实盘或公网上传代码。",
            "可移植源码修订、本地数据绑定及本次动态任务尚未独立验收。",
            "取消保留原子检查点；终态发布前取消不返回成功结果。",
        ),
    )
