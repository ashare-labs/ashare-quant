"""Bounded multi-session native adapter. Assumptions never become owner facts."""

import sys
from datetime import date, datetime, time

import numpy as np
from rqalpha.const import MARKET
from rqalpha.core.strategy_universe import StrategyUniverse
from rqalpha.interface import ExchangeRate
from rqalpha.model.instrument import Instrument

from ashare_quant.execution_six.contracts import owner_price
from ashare_quant.execution_six.native import Board as BaseBoard
from ashare_quant.execution_six.native import Proxy as BaseProxy
from ashare_quant.execution_six.native import ScopedInstrument, day
from ashare_quant.execution_six.native import Source as BaseSource

from .fee_host import FeeAPI
from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod


class CarryInstrument(ScopedInstrument):
    def __init__(self, host):
        self.host = host
        Instrument.__init__(
            self,
            dict(
                order_book_id=host.case.security,
                symbol=host.case.security,
                type="CS",
                exchange=host.case.security.rsplit(".", 1)[1],
                round_lot=100,
                board_type="MainBoard",
                market_tplus=1,
            ),
        )

    @property
    def de_listed_date(self):
        if sys._getframe(1).f_code is not StrategyUniverse._clear_de_listed.__code__:
            raise RuntimeError("CARRY_ABSOLUTE_DELISTING_DATE_UNKNOWN")
        # A native cleanup horizon only. Not exposed to any strategy or owner fact.
        self.host.emit(
            "research_identity_horizon",
            historical_delisting_date=None,
            basis="explicit_research_assumption",
            matching=False,
        )
        return datetime.combine(date.fromisoformat(self.host.metadata["calendar"][-1]), time.max)


class Source(BaseSource):
    def __init__(self, host):
        self.host, self.instrument = host, CarryInstrument(host)

    def available_data_range(self, frequency):
        if frequency != "1m":
            raise RuntimeError("CARRY_ONLY_MINUTE_EXECUTION")
        return self.host.case.day, date.fromisoformat(self.host.case.end_day)

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
        self.host.security(instrument.order_book_id)
        previous = self.host.metadata["calendar_links"][str(self.host.at.date())][
            "previous_session"
        ]
        if not (
            bar_count == 1 and frequency == "1d" and fields == "close" and str(day(dt)) == previous
        ):
            raise RuntimeError("CARRY_INITIALIZATION_HISTORY_SCOPE_REQUIRED")
        value = self.host.read("previous_close")
        return np.array([owner_price(value["close"])], dtype="float64")

    def is_suspended(self, order_book_id, dates):
        self.host.security(order_book_id)
        if any(day(d) != self.host.at.date() for d in dates):
            raise RuntimeError("CARRY_STATUS_DATE_OUT_OF_SCOPE")
        return [self.host.read("quote")["suspended"] for _ in dates]

    def is_st_stock(self, order_book_id, dates):
        self.host.security(order_book_id)
        if any(day(d) != self.host.at.date() for d in dates):
            raise RuntimeError("CARRY_STATUS_DATE_OUT_OF_SCOPE")
        return [self.host.read("quote")["is_st"] for _ in dates]

    def no_action(self, kind):
        if str(self.host.at.date()) not in self.host.case.sessions[:2]:
            raise RuntimeError("CARRY_ACTION_MODEL_OUTSIDE_HOLDING_WINDOW")
        self.host.emit(
            "research_action_model",
            category=kind,
            basis="explicit_research_assumption",
            historical_absence_certified=False,
            owner_unknowns=self.host.provider.facts["required_unknowns"],
        )
        return None

    def get_dividend(self, instrument):
        self.host.security(instrument.order_book_id)
        return self.no_action("cash_dividend")

    def get_split(self, instrument):
        self.host.security(instrument.order_book_id)
        return self.no_action("split")

    def get_share_transformation(self, order_book_id):
        self.host.security(order_book_id)
        return self.no_action("share_transformation")

    def get_exchange_rate(self, trading_date, local, settlement=None):
        if local != MARKET.CN or settlement not in {None, MARKET.CN}:
            raise RuntimeError("CARRY_CROSS_CURRENCY_OUT_OF_SCOPE")
        return ExchangeRate(*([1.0] * 6))


class Proxy(BaseProxy):
    def adjacent(self, dt, field, n):
        if n != 1:
            raise RuntimeError("CARRY_CALENDAR_DISTANCE_OUT_OF_SCOPE")
        row = self._data_source.host.metadata["calendar_links"].get(str(day(dt)))
        if row is None or row["status"] != "supported":
            raise RuntimeError("CARRY_CALENDAR_ENDPOINT_UNKNOWN")
        return datetime.fromisoformat(row[field])

    def get_previous_trading_date(self, dt, n=1, *args, **kwargs):
        return self.adjacent(dt, "previous_session", n)

    def get_next_trading_date(self, dt, n=1, *args, **kwargs):
        return self.adjacent(dt, "next_session", n)


class Board(BaseBoard):
    def get_last_price(self, sid):
        self.host.security(sid)
        if self.host.at.strftime("%H:%M") == "09:00":
            return owner_price(self.host.read("previous_close")["close"])
        return super().get_last_price(sid)


class API(FeeAPI):
    def request(self, quantity, label=None):
        from rqalpha.api import order_shares
        from rqalpha.model.order import MarketOrder

        h = self._host
        if (
            h.submissions >= len(h.intents)
            or (str(self.day), quantity, label) != h.intents[h.submissions]
        ):
            raise RuntimeError("CARRY_ORIGINAL_INTENT_MISMATCH")
        if self.clock != "10:00":
            raise RuntimeError("CARRY_ORDER_CLOCK_MISMATCH")
        if h.case.case_id == "B33":
            quote = h.read("quote", consumer="strategy")
            h.emit(
                "decision",
                label=label,
                api="order",
                security=h.case.security,
                requested=str(quantity),
                last_price=quote["last"],
                paused=quote["suspended"],
                closeable_amount=self.sellable,
                cash_before=str(self.available_cash),
                nav_before=str(h.env.portfolio.total_value),
            )
        h.submissions += 1
        h.emit(
            "order_request",
            quantity=quantity,
            style="market",
            limit_price=None,
            api="order",
            security=h.case.security,
            requested=str(quantity),
            label=label,
        )
        order = order_shares(h.case.security, quantity, price_or_style=MarketOrder())
        if order is not None:
            h.orders[str(order.order_id)] = order
        h.emit(
            "order_return",
            label=label,
            returned_none=order is None,
            order=None if order is None else h.project_order(order),
        )
        return order


class Host(FeeHost):
    proxy_class = Proxy

    def __init__(self, *args):
        super().__init__(*args)
        self.api = API(self)
        labels = ("buy_day_one", "sell_next_day") if self.case.case_id == "B33" else (None, None)
        self.intents = [
            ("2024-03-04", self.case.quantity, labels[0]),
            ("2024-03-05", -self.case.quantity, labels[1]),
        ]
        if self.case.case_id == "B33":
            self.intents.insert(1, ("2024-03-04", -self.case.quantity, "sell_same_callback"))

    def require_flat(self):
        account = self.env.portfolio.stock_account
        if (
            any(p.quantity or p.dividend_receivable for p in account.get_positions())
            or self.env.broker.get_open_orders()
        ):
            raise RuntimeError("CARRY_CASH_ONLY_SESSION_REQUIRES_NATIVE_LIQUIDATION")

    def before(self):
        if str(self.at.date()) == "2024-03-06":
            self.require_flat()
        self.emit(
            "day_start",
            previous_date=self.metadata["calendar_links"][str(self.at.date())]["previous_session"],
        )
        # Refresh actual holdings only, through the native read-only mark hook.
        # With an empty account no first-day 09:00 quote or 3/1 close is required.
        if any(p.quantity for p in self.env.portfolio.stock_account.get_positions()):
            self.env.portfolio.stock_account._on_bar(None)
        self.snapshot("day_start")

    def snapshot(self, label):
        today = str(self.at.date())
        self.emit(
            "snapshot",
            label=label,
            account=self.account(),
            orders=[
                self.project_order(o)
                for o in self.orders.values()
                if str(o.datetime.date()) == today
            ],
            trades=[t for t in self.trades if t["at"][:10] == today],
            open_order_ids=[str(o.order_id) for o in self.env.broker.get_open_orders()],
        )

    def mark_to_close(self, event):
        if str(self.at.date()) == "2024-03-06":
            self.require_flat()
            self.emit("cash_only_no_market_mark", missing_quote=True, native_positions_empty=True)
        else:
            super().mark_to_close(event)

    def after(self):
        self.snapshot("end_of_day")
        if str(self.at.date()) != "2024-03-06" and self.case.case_id in {
            "B14",
            "B16",
            "B17",
            "B18",
        }:
            self.emit("daily_valuation", value=self.read("valuation"))

    def complete(self):
        result = super().complete()
        quantity = self.case.quantity
        returns = [e for e in self.records if e["kind"] == "order_return"]
        filled = len(self.orders) == 2 and all(
            o.filled_quantity == quantity for o in self.orders.values()
        )
        filled = filled and sum(t["quantity"] for t in self.trades) == quantity * 2
        flat = not any(p.quantity for p in self.env.portfolio.stock_account.get_positions())
        triggered = (
            self.submissions == len(self.intents)
            and filled
            and flat
            and not any(e["kind"] == "not_triggered" for e in self.records)
        )
        if self.case.case_id == "B33":
            triggered = (
                triggered
                and len(returns) == 3
                and returns[1]["returned_none"]
                and any(
                    e["kind"] == "native_order_event" and e["event"] == "ORDER_CREATION_REJECT"
                    for e in self.records
                )
            )
        result.update(
            scenario_triggered=bool(triggered),
            status="SCENARIO_EXECUTED" if triggered else "SCENARIO_NOT_TRIGGERED",
            research_classification="CONDITIONAL_RESEARCH_EXECUTION",
            model=self.provider.model,
            known_facts=self.provider.facts,
            unknowns=self.provider.facts["required_unknowns"],
            assumptions=self.provider.model["hypotheses"],
            data_gaps=self.provider.binding["gaps"],
            historical_pit=False,
            general_engine_equivalence=False,
            holdings_overnight_historically_certified=False,
            original_requests_submitted=self.submissions,
            native_liquidation_verified=flat,
        )
        return result


class Mod(FeeMod):
    source_type = Source

    def start_up(self, env, config):
        super().start_up(env, config)
        env.set_price_board(Board(self.host))


def load_mod():
    return Mod()
