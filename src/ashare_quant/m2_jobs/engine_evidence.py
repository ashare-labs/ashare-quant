"""Check committed engine price documents; never authorize or perform a data read.

Recovery can produce another fresh owner read of the same immutable daily fact.
Each authorization belongs to its recorded context/generation. Keeping that old
authorization as evidence does not make it authority for the current execution.
"""

from datetime import datetime, time

from ashare_data import DataError, M2ReadContext
from ashare_data.m2_payloads import economic_payload
from ashare_data.m2_types import canonical_hash

from ashare_quant.contracts import SHANGHAI
from ashare_quant.m2.journal import Session
from ashare_quant.m2.owner_contracts import OwnerDocuments
from ashare_quant.m2.owner_wire import owner_binding, unpack, validate_binding
from ashare_quant.m2_results.service import _require, _sha

from .contracts import HistoricalEnginePriceRead


def historical_price_reads(manifest, receipt):
    """Verify the sealed record against its original Session and event observations.

    This pure projection deliberately does not call Store, use.verify/revalidate,
    OwnerClient.reopen or any engine API. New reads still pass the unchanged
    executor's current-context owner authorization.
    """
    try:
        return _historical_price_reads(manifest, receipt)
    except DataError as exc:
        raise ValueError(
            "Committed engine price documents fail public owner codec validation"
        ) from exc


def _historical_price_reads(manifest, receipt):
    session = Session.model_validate(manifest.session)
    desc, plan, envelope = validate_binding(session.entry)
    binding = owner_binding(session.entry, session.request, session.run_id).to_dict()
    dates = [d.trading_date for d in session.entry.windows[0].days]
    observations = receipt["native_observations"]
    prices, facts, used = [], {}, set()
    for packet in receipt["owner_reads"]:
        read = unpack(OwnerDocuments.model_validate(packet["documents"]))
        docs = read.to_dict()
        q, r, e, a = (docs[k] for k in ("query", "result", "evidence", "authorization"))
        operation = q["operation"]
        if operation not in {"execution_bar", "valuation_bar"}:
            continue
        context = M2ReadContext.from_dict(packet["context"])
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
                schema="m2.query.v1",
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
            and r["schema"] == "m2.result.v1"
            and r["price_basis"] == "raw_unadjusted"
            and r["price_unit"] == "CNY/share"
            and r["volume_unit"] == "share"
            and r["amount_unit"] == "CNY"
            and r["bar_start"] is None
            and r["bar_end"] is None
        )
        expected = dict(
            schema="m2.read-evidence.v1",
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
            schema="m2.read-authorization.v1",
            evidence_sha256=e["evidence_sha256"],
            context=c,
            authorization_kind="fresh_read",
            data_read_permission="conditional_research",
            execution_permission=False,
        )
        _require(a == {**expected_auth, "authorization_sha256": canonical_hash(expected_auth)})

        # A packet must have its own actual committed observation, not just a
        # rehashed context. Restore witnesses delimit generations in that log.
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
