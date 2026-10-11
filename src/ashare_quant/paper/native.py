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
from rqalpha.model.instrument import Instrument
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade

from ashare_quant.m2_jobs.files import atomic, digest
from rqprobe.checkpoint import AtomicFile, Counter, StrictHelper
from rqprobe.guards import block_rqdata_apis

from .account import install
from .contracts import CloseInput, Decision, SessionConfig
from .risk import close_gate, decision_gate, indexed, pretrade, reconcile, require

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
            cursor = self.base + offset
            if cursor <= self.cursor:
                continue
            at = datetime.fromisoformat(f"{self.day}T{clock}")
            yield Event(EVENT[kind], calendar_dt=at, trading_dt=at)
            self.cursor = cursor
            self.env.persist_helper.persist()
            fault(f"event_committed:{offset}")


class DaySource(AbstractDataSource):
    def __init__(self, env, config, decision, close):
        self.env, self.config, self.decision = env, config, decision
        self.before, self.after = (
            indexed(decision.rows, config.universe),
            indexed(close.rows, config.universe),
        )
        # Synthetic instrument facts only; the observed-data gateway remains closed.
        self.instruments = {
            sid: Instrument(
                dict(
                    order_book_id=sid,
                    symbol="SYNTHETIC",
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
            "limit_up": float(facts.upper_limit),
            "limit_down": float(facts.lower_limit),
        }

    def get_open_auction_bar(self, instrument, dt):
        require(
            dt.date() == self.decision.trading_date and dt.hour == 9 and dt.minute == 30,
            "PAPER_OPEN_ROLE",
        )
        sid = instrument.order_book_id
        # Daily opening-price research model only: no invented intraday OHLCV.
        return {
            "datetime": dt,
            "open": float(self.after[sid].open),
            "limit_up": float(self.before[sid].upper_limit),
            "limit_down": float(self.before[sid].lower_limit),
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
        return [self.before[sid].suspended] * len(dates)

    def is_st_stock(self, sid, dates):
        require(sid in self.before, "PAPER_SECURITY_OUT_OF_SCOPE")
        return [self.before[sid].is_st] * len(dates)

    def get_dividend(self, instrument):
        require(self.before[instrument.order_book_id].actions_known_absent, "PAPER_ACTIONS_UNKNOWN")
        return None

    get_split = get_dividend

    def get_share_transformation(self, sid):
        require(self.before[sid].actions_known_absent, "PAPER_ACTIONS_UNKNOWN")
        return None

    def get_exchange_rate(self, trading_date, local, settlement=None):
        require(getattr(local, "value", local) == "CNY", "PAPER_CURRENCY_UNSUPPORTED")
        return 1.0


class Board(AbstractPriceBoard):
    def __init__(self, source):
        self.source = source

    def get_last_price(self, sid):
        s = self.source
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
            "risk": None,
            "submitted": False,
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
        for event in (
            EVENT.POST_BEFORE_TRADING,
            EVENT.POST_OPEN_AUCTION,
            EVENT.POST_BAR,
            EVENT.POST_AFTER_TRADING,
        ):
            env.event_bus.add_listener(event, self.observe)
        env.event_bus.add_listener(EVENT.PRE_BAR, self.cancel_unfilled)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
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
        facts = indexed(self.decision.rows, self.session.universe)
        for sid, delta in sorted(deltas, key=lambda x: (x[1] >= 0, x[0])):
            if not delta:
                continue
            self.submitting = (sid, abs(delta), "BUY" if delta > 0 else "SELL")
            limit = facts[sid].upper_limit if delta > 0 else facts[sid].lower_limit
            result = order_shares(sid, delta, price_or_style=LimitOrder(float(limit)))
            if result is not None:
                self.owned[result.order_id] = result
            else:
                self.state.setdefault("refusals", []).append({"security": sid, "quantity": delta})
            self.submitting = None
            fault("inside_native_submit")
        self.state["submitted"] = True

    def finish(self, _):
        self.get_state()
        after = snapshot(self.env, self.session.universe)
        reconciliation = reconcile(self.state["before"], after, self.state["trades"])
        require(
            not after["open_orders"]
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
            "evidence_kind": "synthetic",
            "native_account_class": type(self.env.portfolio.stock_account).__name__,
            "execution_model": self.session.model,
            "liquidity_claim": False,
            "target_gaps": {
                sid: self.decision.targets[sid] - after["positions"][sid]["quantity"]
                for sid in self.session.universe
                if self.decision.targets[sid] != after["positions"][sid]["quantity"]
            },
        }
        self.env.persist_helper.receipt = result
        self.env.persist_helper.complete = True
        self.env.persist_helper.persist()
        fault("after_complete_checkpoint")
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

    require(engine_pin() == request["engine_pin"], "PAPER_RUNTIME_CHANGED")
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
    session = SessionConfig.model_validate(request["session"])
    decision = Decision.model_validate(request["decision"])
    close = CloseInput.model_validate(request["close"])
    decision_gate(session, decision)
    close_gate(session, decision, close)
    require(
        request["input_id"]
        == digest(
            {"decision": digest(decision), "close": digest(close), "intent": close.intent_id}
        ),
        "PAPER_INPUT_CHANGED",
    )
    runtime.update(
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
                "volume_limit": False,
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
