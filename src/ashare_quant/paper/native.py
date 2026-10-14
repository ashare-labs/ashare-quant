"""One isolated RQAlpha invocation advances one new day, or its uncommitted tail.

No historical replay or balance reconstruction. The previous full native state
is restored before the next day's events. All inputs here are controlled fixtures
until an independently admitted AD execution adapter is available.
"""

import argparse
import json
import os
import signal
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from rqprobe.guards import install_network_guard

install_network_guard()

# ruff: noqa: E402 -- guard network before loading the native runtime.
import numpy as np
import pandas as pd
import rqalpha.main
import rqalpha.utils.config as configs
from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION, TRADING_CALENDAR_TYPE
from rqalpha.core.events import EVENT, Event
from rqalpha.core.executor import Executor
from rqalpha.environment import Environment
from rqalpha.interface import (
    AbstractDataSource,
    AbstractEventSource,
    AbstractFrontendValidator,
    AbstractMod,
    AbstractPriceBoard,
)
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderCancelled
from rqalpha.model.instrument import Instrument
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade

from ashare_quant.contracts import SHANGHAI
from ashare_quant.m2_jobs.files import atomic, digest
from rqprobe.checkpoint import AtomicFile, Counter, StrictHelper
from rqprobe.guards import block_rqdata_apis

from .account import install
from .contracts import CloseInput, Decision, SessionConfig
from .risk import (
    close_gate,
    decision_gate,
    indexed,
    order_admission,
    pretrade,
    reconcile,
    reconcile_opening,
    require,
)

runtime = {}
__config__ = {"priority": 1000}


def fault(stage):
    if os.environ.get("PAPER_FAULT") == stage:
        os.kill(os.getpid(), signal.SIGKILL)


class SettledExecutor(Executor):
    """Preserve native settlement exactly once at the append boundary."""

    def __init__(self, env):
        super().__init__(env)
        self.settled = None
        self.settlement_count = 0

    def get_state(self):
        return json.dumps(
            {
                "schema": "paper.executor.v1",
                "native": super().get_state().decode(),
                "settled": self.settled,
                "count": self.settlement_count,
            }
        ).encode()

    def set_state(self, state):
        value = json.loads(state)
        require(value["schema"] == "paper.executor.v1", "PAPER_EXECUTOR_STATE")
        super().set_state(value["native"].encode())
        self.settled, self.settlement_count = value["settled"], value["count"]

    def _split_and_publish(self, event):
        if event.event_type == EVENT.SETTLEMENT:
            day = self._env.trading_dt.date().isoformat()
            if self.settled == day:
                return  # RQ's next BEFORE_TRADING would otherwise settle the previous day again.
            require(self.settled is None or day > self.settled, "PAPER_SETTLEMENT_REGRESSION")
            super()._split_and_publish(event)
            fault("inside_native_settlement")
            self.settled, self.settlement_count = day, self.settlement_count + 1
        else:
            super()._split_and_publish(event)


class DayEvents(AbstractEventSource):
    def __init__(self, env, base_cursor, day):
        self.env, self.base, self.day, self.cursor = env, base_cursor, day, base_cursor

    def get_state(self):
        return json.dumps({"cursor": self.cursor}).encode()

    def set_state(self, state):
        cursor = json.loads(state)["cursor"]
        require(type(cursor) is int and self.base <= cursor <= self.base + 4, "PAPER_EVENT_CURSOR")
        self.cursor = cursor

    def events(self, start_date, end_date, frequency):
        require(start_date == end_date == self.day and frequency == "1d", "PAPER_DAY_SCOPE")
        for offset, (kind, clock) in enumerate(
            (
                ("BEFORE_TRADING", "09:00:00"),
                ("OPEN_AUCTION", "09:30:00"),
                ("BAR", "15:30:00"),
                ("AFTER_TRADING", "15:30:00"),
            ),
            1,
        ):
            if runtime.get("phase") == "opening" and offset == 3:
                # End the process at a committed native event boundary. Returning
                # from RQ's event iterator would run end-of-day settlement early.
                self.env.mod_dict["paper"].emit_receipt(complete=False)
                os._exit(0)
            cursor = self.base + offset
            if cursor <= self.cursor:
                continue
            at = datetime.fromisoformat(f"{self.day}T{clock}")
            if runtime.get("intraday") and offset == 1:
                at = runtime["decision"].received_at.astimezone(SHANGHAI).replace(tzinfo=None)
            if runtime.get("online") and offset >= 2:
                packet = runtime["opening"] if offset == 2 else runtime["close"]
                at = packet.received_at.astimezone(SHANGHAI).replace(tzinfo=None)
            yield Event(EVENT[kind], calendar_dt=at, trading_dt=at)
            self.cursor = cursor
            self.env.persist_helper.persist()
            fault(f"event_committed:{offset}")


class DaySource(AbstractDataSource):
    def __init__(self, env, config, decision, close):
        self.env, self.config, self.decision = env, config, decision
        self.before, self.after = (
            indexed(decision.rows, config.universe),
            indexed((close or runtime["opening"]).rows, config.universe),
        )
        # Synthetic instrument facts only; the observed-data gateway remains closed.
        self.instruments = {
            sid: Instrument(
                dict(
                    order_book_id=sid,
                    symbol="SCOPED_PAPER" if runtime.get("online") else "SYNTHETIC",
                    type="CS",
                    exchange=sid.split(".")[1],
                    round_lot=100,
                    board_type="MainBoard",
                    market_tplus=1,
                    listed_date="2000-01-01",
                    de_listed_date="2999-12-31",
                )
            )
            for sid in config.universe
        }

    def get_instruments(self, id_or_syms=None, types=None):
        ids = self.config.universe if id_or_syms is None else id_or_syms
        require(set(ids) <= set(self.instruments), "PAPER_SECURITY_OUT_OF_SCOPE")
        return [
            self.instruments[s] for s in ids if types is None or self.instruments[s].type in types
        ]

    def get_trading_calendars(self):
        d = self.decision
        return {
            TRADING_CALENDAR_TYPE.CN_STOCK: pd.DatetimeIndex(
                [d.previous_open, d.trading_date, d.next_open]
            )
        }

    def available_data_range(self, frequency):
        require(frequency == "1d", "PAPER_DAILY_ONLY")
        return self.decision.trading_date, self.decision.trading_date

    def get_bar(self, instrument, dt, frequency):
        require(
            frequency == "1d"
            and dt.date() == self.decision.trading_date
            and self.env.calendar_dt.hour >= 15,
            "PAPER_EXECUTION_ROLE",
        )
        sid = instrument.order_book_id
        r, facts = self.after[sid], self.before[sid]
        return {
            **{k: float(getattr(r, k)) for k in ("open", "high", "low", "close")},
            "datetime": int(dt.strftime("%Y%m%d") + "000000"),
            "volume": r.volume,
            "limit_up": float(facts.upper_limit) if facts.upper_limit is not None else np.nan,
            "limit_down": float(facts.lower_limit) if facts.lower_limit is not None else np.nan,
        }

    def get_open_auction_bar(self, instrument, dt):
        from .intraday_contracts import continuous

        require(
            dt.date() == self.decision.trading_date
            and (
                continuous(dt.replace(tzinfo=SHANGHAI))
                if runtime.get("intraday")
                else dt.hour == 9
                and (30 <= dt.minute <= 35 if runtime.get("online") else dt.minute == 30)
            ),
            "PAPER_OPEN_ROLE",
        )
        sid = instrument.order_book_id
        # Intraday uses the explicitly proved post-intent minute-close + tick.
        # OPEN_AUCTION is only the native broker callback; no old exchange open
        # or invented OHLCV is supplied to that independent execution model.
        return {
            "datetime": dt,
            "open": float(self.after[sid].open),
            "limit_up": float(self.before[sid].upper_limit)
            if self.before[sid].upper_limit is not None
            else np.nan,
            "limit_down": float(self.before[sid].lower_limit)
            if self.before[sid].lower_limit is not None
            else np.nan,
        }

    def history_bars(
        self,
        instrument,
        bar_count,
        frequency,
        fields,
        dt,
        skip_suspended=True,
        include_now=False,
        adjust_type="pre",
        adjust_orig=None,
    ):
        require(
            bar_count == 1
            and frequency == "1d"
            and fields == "close"
            and dt.date() <= self.decision.previous_open,
            "PAPER_PRIOR_CLOSE_ROLE",
        )
        return np.array([float(self.before[instrument.order_book_id].previous_close)])

    def is_suspended(self, sid, dates):
        require(sid in self.before, "PAPER_SECURITY_OUT_OF_SCOPE")
        if runtime.get("online") and runtime["opening"].execution_admission is not None:
            # The original conditional decision retains suspended=None. Only
            # the independently gated opening proof supplies executable state.
            facts = indexed(runtime["opening"].execution_admission.rows, self.before)
            return [facts[sid].suspended] * len(dates)
        return [self.before[sid].suspended] * len(dates)

    def is_st_stock(self, sid, dates):
        require(sid in self.before, "PAPER_SECURITY_OUT_OF_SCOPE")
        return [self.before[sid].is_st] * len(dates)

    def get_dividend(self, instrument):
        events = [a for a in self.decision.cash_dividends if a.security == instrument.order_book_id]
        if events:

            def integer(d):
                return int(d.strftime("%Y%m%d"))

            return np.array(
                [
                    (
                        integer(a.record_date),
                        integer(a.ex_date),
                        integer(a.pay_date),
                        float(a.cash_per_share) * 10,
                        10,
                    )
                    for a in events
                ],
                dtype=[
                    ("book_closure_date", "i8"),
                    ("ex_dividend_date", "i8"),
                    ("payable_date", "i8"),
                    ("dividend_cash_before_tax", "f8"),
                    ("round_lot", "i8"),
                ],
            )
        row = self.before[instrument.order_book_id]
        require(
            row.actions_known_absent or row.action_effect == "zero_entitlement",
            "PAPER_ACTIONS_UNKNOWN",
        )
        return None

    def get_split(self, instrument):
        return None  # This policy admits cash-only actions, never share transformations.

    def get_share_transformation(self, sid):
        require(
            self.before[sid].actions_known_absent
            or self.before[sid].action_effect in {"zero_entitlement", "native_cash_dividend"},
            "PAPER_ACTIONS_UNKNOWN",
        )
        return None

    def get_exchange_rate(self, trading_date, local, settlement=None):
        require(getattr(local, "value", local) == "CNY", "PAPER_CURRENCY_UNSUPPORTED")
        return 1.0


class Board(AbstractPriceBoard):
    def __init__(self, source):
        self.source = source

    def get_last_price(self, sid):
        s = self.source
        if runtime.get("intraday") and s.env.calendar_dt < runtime[
            "opening"
        ].received_at.astimezone(SHANGHAI).replace(tzinfo=None):
            return float(s.before[sid].previous_close)
        if (
            s.env.calendar_dt.date() < s.decision.trading_date
            or s.env.calendar_dt.hour < 9
            or (s.env.calendar_dt.hour == 9 and s.env.calendar_dt.minute < 30)
        ):
            return float(s.before[sid].previous_close)
        if s.env.calendar_dt.hour < 15:
            return float(s.after[sid].open)
        return float(s.after[sid].close)

    def get_limit_up(self, sid):
        return float(self.source.before[sid].upper_limit)

    def get_limit_down(self, sid):
        return float(self.source.before[sid].lower_limit)


class DailyOpenMatcher(DefaultBarMatcher):
    def _get_liquidity_limited_fill(self, order, instrument, open_auction=False):
        if not runtime.get("intraday_partial"):
            return super()._get_liquidity_limited_fill(order, instrument, open_auction)
        rows = indexed(runtime["opening"].intraday.rows, runtime["session"].universe)
        plan = runtime["decision"].strategy_receipt["intraday_plan"]
        volume = rows[order.order_book_id].volume
        lot = int(instrument.round_lot)
        capacity = int(Decimal(volume) * Decimal(plan["max_minute_participation"])) // lot * lot
        remaining = capacity - self._turnover[order.order_book_id]
        if remaining <= 0:
            raise OrderCancelled(
                "Model minute capacity has no remaining whole lot; remainder cancelled"
            )
        return min(order.unfilled_quantity, remaining)

    def _handle_unfilled_order(self, account, order, open_auction):
        if runtime.get("intraday_partial"):
            raise OrderCancelled("Model minute capacity reached; unfilled remainder cancelled")
        return super()._handle_unfilled_order(account, order, open_auction)

    def _get_deal_price(self, order, instrument, open_auction=False):
        # Native broker reclassifies unfilled opening orders as ordinary open orders.
        # A later submission in the same callback must still use raw open, never D close.
        return self._env.data_proxy.get_open_auction_bar(
            order.order_book_id, self._env.calendar_dt
        ).open


def order_projection(order):
    return {
        "id": order.order_id,
        "security": order.order_book_id,
        "side": order.side.name,
        "quantity": order.quantity,
        "filled": order.filled_quantity,
        "remaining": order.unfilled_quantity,
        "status": order.status.name,
        "cost": str(order.transaction_cost),
        "average_price": str(order.avg_price),
        "message": order.message,
    }


def snapshot(env, universe):
    account = env.portfolio.stock_account
    positions = {}
    for sid in universe:
        p = env.portfolio.get_position(sid, POSITION_DIRECTION.LONG)
        positions[sid] = {
            "quantity": p.quantity,
            "sellable": p.closable,
            "price": str(p.last_price),
            "market_value": str(p.market_value),
            "average_cost": str(p.avg_price),
            "dividend_receivable": str(p.dividend_receivable),
        }
    return {
        "total_cash": str(account.total_cash),
        "available_cash": str(account.cash),
        "frozen_cash": str(account.frozen_cash),
        "nav": str(env.portfolio.total_value),
        "positions": positions,
        "open_orders": [order_projection(o) for o in env.broker.get_open_orders()],
    }


class IntentGuard(AbstractFrontendValidator):
    def __init__(self, host):
        self.host = host

    def validate_submission(self, order, account=None):
        h = self.host
        require(
            h.submitting == (order.order_book_id, order.quantity, order.side.name),
            "PAPER_UNPLANNED_ORDER",
        )
        if h.session.risk.profile == "target_then_native_cash_v1":
            check = order_admission(
                h.session,
                snapshot(h.env, h.session.universe),
                order.order_book_id,
                order.quantity if order.side.name == "BUY" else -order.quantity,
                order.frozen_price,
            )
            return check["message"] if check["status"] == "rejected" else None
        if order.side.name == "BUY":
            cost = Decimal(str(order.frozen_price)) * order.quantity
            fee = max(
                Decimal(h.session.minimum_commission), cost * Decimal(h.session.commission_rate)
            )
            require(
                Decimal(str(account.cash)) - cost - fee
                >= Decimal(str(h.env.portfolio.total_value)) * Decimal(h.session.risk.cash_buffer),
                "PAPER_CASH_BUFFER",
            )
        else:
            p = h.env.portfolio.get_position(order.order_book_id, POSITION_DIRECTION.LONG)
            require(order.quantity <= p.closable, "PAPER_T1_LIMIT")

    def validate_cancellation(self, order, account=None):
        return None


class PaperMod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.config = env, config
        self.session, self.decision, self.close = (
            runtime["session"],
            runtime["decision"],
            runtime["close"],
        )
        self.identity, self.input_id = runtime["identity"], runtime["input_id"]
        self.day = self.decision.trading_date.isoformat()
        self.state = {
            "day": self.day,
            "input_id": self.input_id,
            "events": [],
            "trades": [],
            "orders": {},
            "before": None,
            "opening_before": None,
            "opening_reconciliation": None,
            "risk": None,
            "submitted": False,
            "submissions": [],
            "refusals": [],
        }
        self.owned = {}
        self.submitting = None
        source = DaySource(env, self.session, self.decision, self.close)
        env.set_data_source(source)
        env.set_price_board(Board(source))
        env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, DailyOpenMatcher(env, env.config.mod.sys_simulation)
        )
        env.set_event_source(DayEvents(env, runtime["base_cursor"], self.decision.trading_date))
        env.set_persist_provider(AtomicFile(config.checkpoint, self.identity))
        install(env)
        block_rqdata_apis()
        env.add_frontend_validator(IntentGuard(self))
        env.event_bus.prepend_listener(EVENT.PRE_BEFORE_TRADING, self.before_actions)
        for event in (
            EVENT.POST_BEFORE_TRADING,
            EVENT.POST_OPEN_AUCTION,
            EVENT.POST_BAR,
            EVENT.POST_AFTER_TRADING,
        ):
            env.event_bus.add_listener(event, self.observe)
        env.event_bus.add_listener(EVENT.PRE_BAR, self.cancel_unfilled)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        env.event_bus.add_listener(EVENT.ORDER_CREATION_REJECT, self.rejected)
        for event in (
            EVENT.ORDER_CREATION_PASS,
            EVENT.ORDER_CANCELLATION_PASS,
            EVENT.ORDER_UNSOLICITED_UPDATE,
        ):
            env.event_bus.add_listener(event, self.order)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)
        env.event_bus.add_listener(EVENT.POST_SYSTEM_RESTORED, self.restored)

    def restored(self, _):
        self.env.portfolio.stock_account.fast_forward(
            orders=list(self.env.broker.get_open_orders())
        )

    def before_actions(self, _):
        self.state["opening_before"] = snapshot(self.env, self.session.universe)

    def cancel_unfilled(self, _):
        # End-of-session remainder cancellation precedes valuation BAR matching.
        for order in list(self.env.broker.get_open_orders()):
            self.env.broker.cancel_order(order)
        fault("inside_native_cancel")

    def get_state(self):
        for order in self.owned.values():
            self.state["orders"][str(order.order_id)] = order_projection(order)
        return json.dumps(self.state).encode()

    def set_state(self, raw):
        state = json.loads(raw)
        if state["day"] == self.day:
            require(state["input_id"] == self.input_id, "PAPER_INPUT_CHANGED")
            self.state = state
        else:
            require(
                state["day"] == self.decision.previous_open.isoformat()
                and runtime["parent_complete"],
                "PAPER_CONTINUATION_PARENT",
            )

    def observe(self, event):
        if event.event_type == EVENT.POST_BEFORE_TRADING:
            self.state["before"] = snapshot(self.env, self.session.universe)
            self.state["opening_reconciliation"] = reconcile_opening(
                self.state["opening_before"], self.state["before"], self.decision
            )
            if self.decision.cash_dividends:
                fault("inside_native_dividend")
            try:
                self.state["risk"] = pretrade(self.session, self.decision, self.state["before"])
            except Exception as exc:
                atomic(
                    Path(self.config.result).with_name("error.json"),
                    {"code": getattr(exc, "code", "PAPER_RISK_FAILED"), "message": str(exc)},
                )
                raise
        self.state["events"].append(
            {
                "event": event.event_type.name,
                "at": str(self.env.calendar_dt),
                "account": snapshot(self.env, self.session.universe),
            }
        )

    def order(self, event):
        self.owned[event.order.order_id] = event.order

    def trade(self, event):
        t = event.trade
        self.state["trades"].append(
            {
                "id": t.exec_id,
                "order_id": t.order_id,
                "security": t.order_book_id,
                "side": t.side.name,
                "quantity": t.last_quantity,
                "price": str(t.last_price),
                "cost": str(t.transaction_cost),
                "commission": str(t.commission),
                "tax": str(t.tax),
                "at": str(t.trading_datetime),
            }
        )
        fault("inside_native_trade")

    def rejected(self, event):
        self.state["refusals"].append(
            {
                "security": event.order_book_id,
                "code": "PAPER_NATIVE_ORDER_REJECTED",
                "message": event.reason,
            }
        )

    def submit(self):
        from rqalpha.api import order_shares
        from rqalpha.model.order import LimitOrder

        require(
            not self.state["submitted"] and self.state["risk"] is not None,
            "PAPER_INTENT_ALREADY_CONSUMED",
        )
        before = self.state["before"]["positions"]
        deltas = [
            (s, self.decision.targets[s] - before[s]["quantity"]) for s in self.session.universe
        ]
        if runtime.get("continuation"):
            require(all(delta == 0 for _, delta in deltas), "PAPER_CONTINUATION_HOLD_ONLY")
        facts = indexed(self.decision.rows, self.session.universe)
        for sid, delta in sorted(deltas, key=lambda x: (x[1] >= 0, x[0])):
            if not delta:
                continue
            from .risk import order_permission

            order_permission(self.session, self.decision, sid, "BUY" if delta > 0 else "SELL")
            self.submitting = (sid, abs(delta), "BUY" if delta > 0 else "SELL")
            before_order = snapshot(self.env, self.session.universe)
            if self.session.risk.profile == "target_then_native_cash_v1":
                check = order_admission(
                    self.session, before_order, sid, delta, self.env.price_board.get_last_price(sid)
                )
                if check["status"] == "rejected":
                    self.state["refusals"].append(check)
                    self.state["submissions"].append(
                        {**check, "order_id": None, "cash_after": before_order["total_cash"]}
                    )
                    self.submitting = None
                    continue
                # Native market order is frozen at the available raw open under
                # the declared daily-open model, matching the fixed strategy runner.
                result = order_shares(sid, delta)
            else:
                limit = facts[sid].upper_limit if delta > 0 else facts[sid].lower_limit
                result = order_shares(sid, delta, price_or_style=LimitOrder(float(limit)))
            if result is not None:
                self.owned[result.order_id] = result
            else:
                if not self.state["refusals"] or self.state["refusals"][-1]["security"] != sid:
                    self.state["refusals"].append(
                        {
                            "security": sid,
                            "quantity": delta,
                            "code": "PAPER_NATIVE_ORDER_REJECTED",
                            "message": "原生引擎未创建订单",
                        }
                    )
            self.state["submissions"].append(
                {
                    "security": sid,
                    "quantity": delta,
                    "order_id": result.order_id if result is not None else None,
                    "cash_before": before_order["total_cash"],
                    "cash_after": snapshot(self.env, self.session.universe)["total_cash"],
                    "status": result.status.name if result is not None else "rejected",
                }
            )
            self.submitting = None
            fault("inside_native_submit")
        self.state["submitted"] = True

    def finish(self, _):
        self.emit_receipt(complete=True)

    def emit_receipt(self, *, complete):
        self.get_state()
        after = snapshot(self.env, self.session.universe)
        reconciliation = reconcile(self.state["before"], after, self.state["trades"])
        require(
            not complete
            or not after["open_orders"]
            and abs(Decimal(after["frozen_cash"])) <= Decimal("0.00000001"),
            "PAPER_UNFINISHED_DAY_ORDERS",
        )
        executor = self.env.persist_helper.objects["executor"]
        result = {
            **self.state,
            "account": after,
            "reconciliation": reconciliation,
            "cursor": self.env.event_source.cursor,
            "settlement_count": executor.settlement_count,
            "parent_checkpoint": runtime["parent_sha"],
            "identity": self.identity,
            "evidence_kind": self.decision.evidence_kind,
            "native_account_class": type(self.env.portfolio.stock_account).__name__,
            "execution_model": self.session.model,
            "liquidity_claim": False,
            "target_gaps": {
                sid: self.decision.targets[sid] - after["positions"][sid]["quantity"]
                for sid in self.session.universe
                if self.decision.targets[sid] != after["positions"][sid]["quantity"]
            },
        }
        if runtime.get("intraday_partial"):
            result["capacity_policy"] = {
                "participation": self.decision.strategy_receipt["intraday_plan"][
                    "max_minute_participation"
                ],
                "basis": "observed_complete_minute_volume",
                "remainder": "cancel_after_minute",
                "exchange_rule": False,
            }
        if runtime.get("continuation"):
            result["continuation_runtime"] = dict(
                manifest_sha256=digest(runtime["continuation"]),
                engine_pin_sha256=digest(runtime["execution_runtime_pin"]),
                original_parent=runtime["continuation"]["parent_checkpoint"],
            )
        if runtime.get("online"):
            result.update(
                phase="closing" if complete else "opening",
                opening_sha256=digest(runtime["opening"]),
                close_sha256=digest(self.close) if complete else None,
                native_clock_timezone="Asia/Shanghai",
            )
        self.env.persist_helper.receipt = result
        self.env.persist_helper.complete = complete
        self.env.persist_helper.persist()
        fault("after_complete_checkpoint" if complete else "after_opening_checkpoint")
        atomic(Path(self.config.result), result)

    def tear_down(self, code, exception=None):
        return None


def submit_targets():
    Environment.get_instance().mod_dict["paper"].submit()


def load_mod():
    return PaperMod()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    from .service import engine_pin

    current_pin = engine_pin()
    if "continuation" not in request:
        require(current_pin == request["engine_pin"], "PAPER_RUNTIME_CHANGED")
    require(
        request["identity"]
        == digest({"config": request["session"], "engine": request["engine_pin"]}),
        "PAPER_SESSION_CHANGED",
    )
    parent_pid = int(os.environ.get("PAPER_PARENT_PID", os.getppid()))

    def orphan_watchdog():
        while True:
            if os.getppid() != parent_pid:
                os._exit(75)
            time.sleep(0.2)

    threading.Thread(target=orphan_watchdog, daemon=True).start()
    online = request.get("online", False)
    opening = None
    if online:
        from .online_contracts import OnlineClose, OnlineDecision, OnlineSession, OpeningInput
        from .online_gates import closing_gate, opening_gate

        session = OnlineSession.model_validate(request["session"])
        decision = OnlineDecision.model_validate(request["decision"])
        if "continuation" in request:
            from .intraday_continuation import verify_request

            verify_request(request, session, decision, current_pin)
        opening = OpeningInput.model_validate(request["opening"])
        close = OnlineClose.model_validate(request["close"]) if request["close"] else None
        require(request["phase"] in {"opening", "closing"}, "PAPER_PHASE")
        require((request["phase"] == "closing") == (close is not None), "PAPER_PHASE_INPUT")
        opening_gate(session, decision, opening)
        if close:
            closing_gate(session, decision, opening, close)
        expected = digest(
            {"decision": digest(decision), "opening": digest(opening), "intent": opening.intent_id}
        )
    else:
        require("continuation" not in request, "PAPER_CONTINUATION_ONLINE_ONLY")
        session = SessionConfig.model_validate(request["session"])
        decision = Decision.model_validate(request["decision"])
        close = CloseInput.model_validate(request["close"])
        close_gate(session, decision, close)
        expected = digest(
            {"decision": digest(decision), "close": digest(close), "intent": close.intent_id}
        )
    decision_gate(session, decision)
    require(request["input_id"] == expected, "PAPER_INPUT_CHANGED")
    runtime.update(
        continuation=request.get("continuation"),
        execution_runtime_pin=current_pin,
        online=online,
        intraday=session.model
        in {
            "prior-close-intent-next-complete-minute.v1",
            "prior-close-intent-minute-partial-cancel.v2",
        },
        intraday_partial=session.model == "prior-close-intent-minute-partial-cancel.v2",
        phase=request.get("phase"),
        opening=opening,
        session=session,
        decision=decision,
        close=close,
        **{
            k: request[k]
            for k in ("identity", "input_id", "base_cursor", "parent_complete", "parent_sha")
        },
    )
    root = Path(args.request).parent
    checkpoint = AtomicFile(root / "native.chk", request["identity"])
    prior = checkpoint.read()
    if prior and prior["complete"] and prior["receipt"]["input_id"] == request["input_id"]:
        atomic(root / "receipt.json", prior["receipt"])
        return
    Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)
    rqalpha.main.PersistHelper = StrictHelper
    rqalpha.main.Executor = SettledExecutor
    rqalpha.main.BaseDataSource = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("PAPER_NO_DEFAULT_BUNDLE")
    )
    configs.user_config = configs.project_config = lambda: {}
    day = decision.trading_date.isoformat()
    source = (
        "from ashare_quant.paper.native import submit_targets\ndef init(context):\n    subscribe("
        + repr(list(session.universe))
        + ")\ndef open_auction(context, bars):\n    submit_targets()\n"
        + "def handle_bar(context, bars):\n    pass\n"
    )
    config = {
        "base": {
            "start_date": day,
            "end_date": day,
            "frequency": "1d",
            "run_type": "b",
            "accounts": {"stock": float(session.initial_cash)},
            "rqdatac_uri": "disabled",
            "auto_update_bundle": False,
            "data_bundle_path": str(root / "ABSENT"),
            "persist": True,
            "persist_mode": "real_time",
            "capital_gain_tax_rate": 0,
            "partial_fill_on_insufficient_cash": False,
        },
        "extra": {"log_level": "error", "locale": "en_US"},
        "mod": {
            "sys_progress": {"enabled": False},
            "sys_analyser": {"enabled": False},
            "sys_simulation": {
                "matching_type": "current_bar",
                "slippage": 0,
                "volume_limit": runtime["intraday_partial"],
                "volume_percent": float(
                    decision.strategy_receipt["intraday_plan"]["max_minute_participation"]
                )
                if runtime["intraday_partial"]
                else 0.25,
                "inactive_limit": False,
            },
            "sys_transaction_cost": {
                "stock_min_commission": float(session.minimum_commission),
                "stock_commission_multiplier": float(
                    Decimal(session.commission_rate) / Decimal("0.0008")
                ),
                # Pinned 6.4.1 uses 0.0005 when pit_tax is disabled.
                "tax_multiplier": float(Decimal(session.sell_tax_rate) / Decimal("0.0005")),
                "pit_tax": False,
            },
            "sys_accounts": {
                "stock_t1": True,
                "dividend_tax_enabled": False,
                "dividend_reinvestment": False,
                "financing_rate": 0,
            },
            "paper": {
                "enabled": True,
                "lib": "ashare_quant.paper.native",
                "priority": 1000,
                "checkpoint": str(root / "native.chk"),
                "result": str(root / "receipt.json"),
            },
        },
    }
    rqalpha.main.run(configs.parse_config(config, source_code=source), source_code=source)
    require((root / "receipt.json").is_file(), "PAPER_NATIVE_RUN_FAILED")


if __name__ == "__main__":
    main()
