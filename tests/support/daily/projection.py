"""Typed dynamic projections from the committed native receipt, never attempt traces."""

import json
from datetime import datetime, time
from decimal import Decimal

from ashare_quant.contracts import SHANGHAI
from ashare_quant.storage.canonical import digest
from ashare_quant.tasks.projections import _account, _documents, _money, _require, _sha
from tests.support.daily.contracts import HistoricalEnginePriceRead, JobResult
from tests.support.legacy_backtests.journal import Session
from tests.support.legacy_provider.owner_contracts import OwnerDocuments
from tests.support.legacy_provider.owner_wire import owner_binding, unpack, validate_binding


def historical_price_reads(manifest, receipt):
    """Verify the sealed record against its original Session and event observations."""
    from ashare_data import DataError

    try:
        return _historical_price_reads(manifest, receipt)
    except DataError as exc:
        raise ValueError(
            "Committed engine price documents fail public owner codec validation"
        ) from exc


def _historical_price_reads(manifest, receipt):
    from ashare_data import PortableReadContext
    from ashare_data.research.payloads import economic_payload
    from ashare_data.research.portable_types import canonical_hash

    session = Session.model_validate(manifest.session)
    desc, plan, envelope = validate_binding(session.entry)
    binding = owner_binding(session.entry, session.request, session.run_id).to_dict()
    dates = [d.trading_date for d in session.days]
    observations = receipt["native_observations"]
    prices, facts, used = [], {}, set()
    for packet in receipt["owner_reads"]:
        read = unpack(OwnerDocuments.model_validate(packet["documents"]))
        docs = read.to_dict()
        q, r, e, a = (docs[k] for k in ("query", "result", "evidence", "authorization"))
        operation = q["operation"]
        if operation not in {"execution_bar", "valuation_bar"}:
            continue
        context = PortableReadContext.from_dict(packet["context"])
        c = context.to_dict()
        _require(
            all(c[k] == v for k, v in binding.items())
            and context.consumer == "engine"
            and context.visibility == "assumed"
            and context.current_date in dates
            and context.epoch == context.restore_generation
            and 0 <= context.restore_generation <= receipt["generation"]
        )
        day = context.current_date.isoformat()
        _require(
            q
            == dict(
                schema="portable.query.v1",
                operation=operation,
                security="600000.XSHG",
                current_date=day,
                target_date=day,
                role="current",
                call=None,
                n=None,
            )
        )
        _require(
            r == economic_payload(r)
            and all(
                r[k] == q[k]
                for k in ("operation", "security", "current_date", "target_date", "role")
            )
            and r["schema"] == "portable.result.v1"
            and r["price_basis"] == "raw_unadjusted"
            and r["price_unit"] == "CNY/share"
            and r["volume_unit"] == "share"
            and r["amount_unit"] == "CNY"
            and r["bar_start"] is None
            and r["bar_end"] is None
        )
        expected = dict(
            schema="portable.read-evidence.v1",
            owner=desc,
            owner_pin_sha256=canonical_hash(desc),
            window_id=session.entry.owner_binding.owner_window_id,
            window_plan_sha256=plan["window_plan_sha256"],
            query_sha256=read.query.sha256,
            result_sha256=read.result.sha256,
            source_references_sha256=read.sources.sha256,
            role="current",
            current_date=day,
            target_date=day,
            quality=envelope["original_quality"],
            assumption_refs=envelope["assumption_refs"],
            source_evidence_kind="source_claim_unverified",
        )
        _require(e == {**expected, "evidence_sha256": canonical_hash(expected)})
        expected_auth = dict(
            schema="portable.read-authorization.v1",
            evidence_sha256=e["evidence_sha256"],
            context=c,
            authorization_kind="fresh_read",
            data_read_permission="conditional_research",
            execution_permission=False,
        )
        _require(a == {**expected_auth, "authorization_sha256": canonical_hash(expected_auth)})
        matches = [
            i
            for i, o in enumerate(observations)
            if i not in used
            and o["kind"] == "owner_engine_read"
            and o["details"]
            == dict(
                operation=operation,
                evidence_sha256=read.evidence.sha256,
                authorization_sha256=read.authorization.sha256,
            )
            and (o["generation"], o["cursor"], o["phase"])
            == (context.restore_generation, context.event_cursor, context.phase)
            and datetime.fromisoformat(o["at"]) == context.logical_at
        ]
        _require(len(matches) == 1)
        index = matches[0]
        used.add(index)
        restores = [o for o in observations[:index] if o["kind"] == "restored_native_state"]
        active_generation = (
            restores[-1]["generation"] if restores else observations[0]["generation"]
        )
        _require(context.restore_generation == active_generation)
        if restores:
            last = restores[-1]
            _require(
                context.event_cursor >= last["cursor"]
                and context.logical_at >= datetime.fromisoformat(last["at"])
            )
        else:
            last = None
        phase = "SUBMIT_MATCH" if operation == "execution_bar" else context.phase
        _require(
            phase
            in (
                {"SUBMIT_MATCH"}
                if operation == "execution_bar"
                else {"AFTER_TRADING", "SETTLEMENT"}
            )
            and context.phase == phase
        )
        expected_at = datetime.combine(
            context.current_date, time(15) if phase == "SUBMIT_MATCH" else time(15, 1), SHANGHAI
        )
        _require(
            context.logical_at == expected_at
            and context.query_end == datetime.combine(context.current_date, time(15), SHANGHAI)
        )
        before = (
            dates.index(context.current_date) * 3
            + {"SUBMIT_MATCH": 1, "AFTER_TRADING": 2, "SETTLEMENT": 3}[phase]
        )
        if context.event_cursor != before:
            _require(
                phase != "SETTLEMENT"
                and context.event_cursor == before + 1
                and last is not None
                and last["cursor"] == context.event_cursor
                and last["details"]["journal_cursor"] == context.event_cursor
                and last["phase"] == phase
                and datetime.fromisoformat(last["at"]) == expected_at
            )
        stable = tuple(packet["documents"][k] for k in ("query", "result", "sources", "evidence"))
        key = (operation, day)
        if key in facts:
            previous, generation, previous_index = facts[key]
            _require(
                stable == previous
                and generation < context.restore_generation
                and previous_index < index
                and last is not None
            )
        facts[key] = stable, context.restore_generation, index
        prices.append(
            HistoricalEnginePriceRead(
                operation=operation,
                trading_date=context.current_date,
                context=c,
                owner_documents=docs,
                owner_document_sha256={k: _sha(v.encode()) for k, v in packet["documents"].items()},
                native_observation_index=index,
            )
        )
    _require(
        used
        == {
            i
            for i, o in enumerate(observations)
            if o["kind"] == "owner_engine_read"
            and o["details"]["operation"] in {"execution_bar", "valuation_bar"}
        }
    )
    return tuple(prices)


def project(manifest, base, proof, evidence, recovery):
    session = manifest.session
    window = manifest.request.window_id
    dates = [d.isoformat() for d in manifest.execution_scope.trading_dates]
    expected_days = len(dates)
    quality = manifest.approved_window.data_identity.original_quality
    from tests.support.legacy_backtests.journal import restore_extension
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
