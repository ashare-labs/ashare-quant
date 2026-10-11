"""Daily portfolio bridge. Native broker, trades and Account are the only ledger."""

import json
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, MARKET, POSITION_DIRECTION, TRADING_CALENDAR_TYPE
from rqalpha.core.events import EVENT
from rqalpha.data.data_proxy import DataProxy
from rqalpha.interface import AbstractDataSource, AbstractMod, AbstractPriceBoard, ExchangeRate
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.model.instrument import Instrument

from rqprobe.checkpoint import AtomicFile, StrictHelper, kill_at
from rqprobe.events import RecordedEventSource
from rqprobe.guards import block_rqdata_apis

from .inputs import digest, require
from .signals import order_plan, scheduled

runtime = {}
__config__ = {"priority": 1000}


class ScopedInstrument(Instrument):
    def __init__(self, host, sid):
        self.host = host
        super().__init__(
            dict(
                order_book_id=sid,
                symbol=sid,
                type="CS",
                exchange=sid.split(".")[1],
                round_lot=100,
                board_type="MainBoard",
                market_tplus=1,
            )
        )

    def listed_at(self, dt):
        d = str(dt.date() if isinstance(dt, datetime) else dt)
        require(d in self.host.data.calendar, "LISTING_OUTSIDE_OWNER_SCOPE")
        return True

    active_at = listed_at

    def de_listed_at(self, dt):
        self.listed_at(dt)
        return False

    @property
    def listed_date(self):
        raise RuntimeError("ABSOLUTE_LISTING_DATE_NOT_CLAIMED")

    @property
    def de_listed_date(self):
        # A native cleanup horizon only; never emitted as an observed listing date.
        return datetime.combine(
            date.fromisoformat(self.host.data.calendar[-1]) + timedelta(days=1), time()
        )


class Source(AbstractDataSource):
    def __init__(self, host):
        self.host = host
        self.instruments = {s: ScopedInstrument(host, s) for s in host.data.ids}

    def get_instruments(self, id_or_syms=None, types=None):
        ids = self.instruments if id_or_syms is None else id_or_syms
        require(all(s in self.instruments for s in ids), "NATIVE_SECURITY_OUT_OF_SCOPE")
        return (
            [self.instruments[s] for s in ids]
            if types is None or INSTRUMENT_TYPE.CS in types
            else []
        )

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: pd.DatetimeIndex(self.host.data.calendar)}

    def available_data_range(self, frequency):
        require(frequency == "1d", "ONLY_DAILY_FREQUENCY")
        return tuple(
            date.fromisoformat(d) for d in (self.host.data.days[0], self.host.data.days[-1])
        )

    def get_bar(self, instrument, dt, frequency):
        require(frequency == "1d", "ONLY_DAILY_FREQUENCY")
        row = self.host.data.row(instrument.order_book_id, str(dt.date()))
        return {
            **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")},
            "datetime": int(dt.strftime("%Y%m%d") + "000000"),
            "limit_up": float(row["upper_limit"]),
            "limit_down": float(row["lower_limit"]),
        }

    def history_bars(self, instrument, bar_count, frequency, fields, dt, **kwargs):
        require(
            bar_count == 1 and frequency == "1d" and fields == "close", "NATIVE_HISTORY_QUERY_SCOPE"
        )
        return np.array(
            [float(self.host.data.row(instrument.order_book_id, str(dt.date()))["close"])]
        )

    def is_suspended(self, sid, dates):
        return [
            self.host.data.row(sid, str(d.date() if isinstance(d, datetime) else d))["suspended"]
            for d in dates
        ]

    def is_st_stock(self, sid, dates):
        raise RuntimeError("ST_STATUS_NOT_REQUIRED_OR_INFERRED")

    def get_dividend(self, instrument):
        rows = [
            a
            for a in self.host.data.actions
            if a["security"] == instrument.order_book_id and a["kind"] == "cash_dividend"
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r["ex_date"])

        def integer(d):
            return int(d.replace("-", ""))

        return np.array(
            [
                (
                    integer(r["record_date"]),
                    integer(r["ex_date"]),
                    integer(r["pay_date"]),
                    float(r["cash_per_share"]) * 10,
                    10,
                )
                for r in rows
            ],
            dtype=[
                ("book_closure_date", "i8"),
                ("ex_dividend_date", "i8"),
                ("payable_date", "i8"),
                ("dividend_cash_before_tax", "f8"),
                ("round_lot", "i8"),
            ],
        )

    def get_split(self, instrument):
        rows = [
            a
            for a in self.host.data.actions
            if a["security"] == instrument.order_book_id and a["kind"] == "split"
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r["ex_date"])
        require(
            all(r["ex_date"] == r["tradable_date"] == r["credit_date"] for r in rows),
            "SEPARATE_SHARE_CREDIT_TRADABILITY_UNSUPPORTED",
        )
        return np.array(
            [(int(r["ex_date"].replace("-", "")) * 1000000, float(r["ratio"]), 1) for r in rows],
            dtype=[
                ("ex_date", "i8"),
                ("split_coefficient_to", "f8"),
                ("split_coefficient_from", "f8"),
            ],
        )

    def get_share_transformation(self, sid):
        return None

    def current_snapshot(self, *args, **kwargs):
        raise RuntimeError("INTRADAY_SNAPSHOT_NOT_IN_DAILY_CONTRACT")

    def get_exchange_rate(self, trading_date, local, settlement=None):
        require(local == MARKET.CN and settlement in {None, MARKET.CN}, "ONLY_CNY")
        return ExchangeRate(*([1.0] * 6))


class Proxy(DataProxy):
    def get_instrument_history(self, id_or_sym, listed_at=None):
        found = self._data_source.get_instruments([id_or_sym])
        if listed_at is not None:
            require(found[0].listed_at(listed_at), "LISTING_OUTSIDE_OWNER_SCOPE")
        return found


class Board(AbstractPriceBoard):
    def __init__(self, host):
        self.host = host

    def get_last_price(self, sid):
        h = self.host
        day = h.day
        if h.phase == "DECIDE":
            return float(h.data.row(sid, h.data.previous[day])["close"])
        return float(h.data.row(sid, day)["open" if h.phase == "EXECUTE" else "close"])

    def get_limit_up(self, sid):
        return float(self.host.data.row(sid, self.host.day)["upper_limit"])

    def get_limit_down(self, sid):
        return float(self.host.data.row(sid, self.host.day)["lower_limit"])


class Matcher(DefaultBarMatcher):
    def __init__(self, host, config):
        self.host = host
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        require(self.host.phase == "EXECUTE" and not open_auction, "DAILY_OPEN_MATCH_PHASE")
        return float(self.host.data.row(order.order_book_id, self.host.day)["open"])


class Helper(StrictHelper):
    def restore(self, event=None):
        # Restore the source clock and journal before native positions touch data.
        keys = ("event_source", "mod_dailyportfolio", "universe")
        require(all(k in self.objects for k in keys), "NATIVE_RESTORE_REGISTRY")
        self.objects = {**{k: self.objects[k] for k in keys}, **self.objects}
        return super().restore(event)


class Mod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.data = env, runtime["data"]
        runtime["mod"] = self
        self.day, self.phase = self.data.days[0], "DECIDE"
        self.decisions, self.orders, self.trades, self.accounts, self.submissions = (
            [],
            {},
            [],
            [],
            [],
        )
        env.set_event_source(RecordedEventSource(env, runtime["records"]))
        env.set_data_source(Source(self))
        env.set_price_board(Board(self))
        env.set_persist_provider(AtomicFile(runtime["root"] / "native.chk", runtime["identity"]))
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.ready)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        env.event_bus.add_listener(EVENT.POST_AFTER_TRADING, self.close_day)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)
        for kind in (
            EVENT.ORDER_PENDING_NEW,
            EVENT.ORDER_CREATION_PASS,
            EVENT.ORDER_CREATION_REJECT,
            EVENT.ORDER_UNSOLICITED_UPDATE,
            EVENT.ORDER_CANCELLATION_PASS,
        ):
            env.event_bus.add_listener(kind, self.order_event)
        block_rqdata_apis()

    def ready(self, event):
        self.env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, Matcher(self, self.env.config.mod.sys_simulation)
        )
        for kind in (
            EVENT.PRE_BEFORE_TRADING,
            EVENT.PRE_BAR,
            EVENT.PRE_AFTER_TRADING,
            EVENT.PRE_SETTLEMENT,
        ):
            self.env.event_bus.prepend_listener(kind, self.phase_event)

    def phase_event(self, event):
        self.day = str(self.env.trading_dt.date())
        self.phase = {
            EVENT.PRE_BEFORE_TRADING: "DECIDE",
            EVENT.PRE_BAR: "EXECUTE",
            EVENT.PRE_AFTER_TRADING: "CLOSE",
            EVENT.PRE_SETTLEMENT: "SETTLE",
        }[event.event_type]

    def decide(self):
        prior = (
            self.accounts[-1]
            if self.accounts
            else {
                "cash": runtime["manifest"]["execution"]["initial_cash"],
                "equity": runtime["manifest"]["execution"]["initial_cash"],
                "positions": {},
            }
        )
        rebalance = scheduled(
            runtime["strategy"], self.day, self.data.previous[self.day], not self.decisions
        )
        history = self.data.history(self.day)
        signal = runtime["callback"](history, prior["equity"]) if rebalance else None
        self.decisions.append(
            {
                "date": self.day,
                "at": self.day + "T09:00:00+08:00",
                "cutoff": self.data.previous[self.day] + "T15:00:00+08:00",
                "history_sha256": digest(history),
                "prior_account_sha256": digest(prior),
                "rebalance": rebalance,
                "signal": signal,
            }
        )
        kill_at("decision_candidate", self.env.event_source.cursor + 1)

    def submit(self):
        from rqalpha.api import order_shares

        decision = self.decisions[-1]
        require(decision["date"] == self.day, "COMMITTED_DECISION_DATE_MISMATCH")
        if not decision["rebalance"]:
            return
        actual = {
            s: int(self.env.portfolio.get_position(s, POSITION_DIRECTION.LONG).quantity)
            for s in self.data.ids
        }
        for sid, quantity in order_plan(decision["signal"]["targets"], actual):
            before = self.account()
            result = order_shares(sid, quantity)
            require(not isinstance(result, list), "UNEXPECTED_ORDER_RESULT_LIST")
            if result is not None:
                self.project_order(result)
            self.submissions.append(
                {
                    "date": self.day,
                    "security": sid,
                    "quantity": quantity,
                    "order_id": str(result.order_id) if result is not None else None,
                    "returned_none": result is None,
                    "cash_before": before["cash"],
                    "cash_after": self.account()["cash"],
                    "suspended": self.data.row(sid, self.day)["suspended"],
                }
            )
            kill_at("native_submit", self.env.event_source.cursor + 1)

    def project_order(self, order):
        row = {
            "id": str(order.order_id),
            "security": order.order_book_id,
            "date": self.day,
            "side": order.side.name,
            "quantity": int(order.quantity),
            "filled": int(order.filled_quantity),
            "status": order.status.name,
            "message": order.message,
        }
        self.orders[row["id"]] = row

    def order_event(self, event):
        order = getattr(event, "order", None)
        if order is not None:
            self.project_order(order)

    def trade(self, event):
        t = event.trade
        self.trades.append(
            {
                "id": str(t.exec_id),
                "order_id": str(t.order_id),
                "date": self.day,
                "at": str(t.trading_datetime),
                "security": t.order_book_id,
                "side": t.side.name,
                "quantity": int(t.last_quantity),
                "price": repr(float(t.last_price)),
                "commission": repr(float(t.commission)),
                "tax": repr(float(t.tax)),
            }
        )

    def account(self):
        p = self.env.portfolio
        positions = {}
        for sid in self.data.ids:
            pos = p.get_position(sid, POSITION_DIRECTION.LONG)
            if pos.quantity or pos.dividend_receivable:
                positions[sid] = {
                    "quantity": int(pos.quantity),
                    "sellable": int(pos.closable),
                    "price": repr(float(pos.last_price)),
                    "value": repr(float(pos.market_value)),
                    "average_price": repr(float(pos.avg_price)),
                    "dividend_receivable": repr(float(pos.dividend_receivable)),
                }
        return {
            "date": self.day,
            "cash": repr(float(p.cash)),
            "equity": repr(float(p.total_value)),
            "positions": positions,
        }

    def close_day(self, event):
        # The daily BAR occurs at open. Explicit native mark refresh at observed close.
        for sid in self.data.ids:
            pos = self.env.portfolio.get_position(sid, POSITION_DIRECTION.LONG)
            pos.update_last_price(float(self.data.row(sid, self.day)["close"]))
        self.accounts.append(self.account())

    def get_state(self):
        return json.dumps(
            {
                k: getattr(self, k)
                for k in (
                    "day",
                    "phase",
                    "decisions",
                    "orders",
                    "trades",
                    "accounts",
                    "submissions",
                )
            },
            sort_keys=True,
        ).encode()

    def set_state(self, raw):
        for k, v in json.loads(raw).items():
            setattr(self, k, v)

    def finish(self, event):
        self.result = {
            "schema": "aq-daily-portfolio-result.v1",
            "identity": runtime["identity"],
            "strategy": runtime["strategy"],
            "window": self.data.window,
            "real_market_data": self.data.document["real_market_data"],
            "historical_pit_verified": False,
            "state_mode": self.data.document.get("state_mode", "synthetic_known"),
            "economic_acceptance": self.data.document.get("economic_acceptance", "synthetic_only"),
            "source_refs": self.data.document["source_refs"],
            "assumptions": self.data.document.get("assumptions", []),
            "jq_comparison": "not_collected",
            "native_account_owner": type(self.env.portfolio.stock_account).__module__,
            "decisions": self.decisions,
            "orders": list(self.orders.values()),
            "trades": self.trades,
            "accounts": self.accounts,
            "submissions": self.submissions,
            "final_account": self.account(),
            "limitations": self.data.document["limitations"],
            "corporate_action_policy": runtime["manifest"]["execution"].get(
                "corporate_action_policy"
            ),
            "action_coverage": self.data.document["action_coverage"],
            "listing_coverage": self.data.document["listing_coverage"],
        }
        self.env.persist_helper.complete, self.env.persist_helper.receipt = True, self.result
        self.env.persist_helper.persist()

    def tear_down(self, code, exception=None):
        return getattr(self, "result", None)


def load_mod():
    return Mod()
