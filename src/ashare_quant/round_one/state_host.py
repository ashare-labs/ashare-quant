"""Native empty-account lifecycle with explicit unknown exchange adjacency."""

import sys
from datetime import date, datetime, time
from types import SimpleNamespace

from rqalpha.core.executor import Executor
from rqalpha.portfolio.account import Account
from rqalpha.portfolio.capital_gains_tax import CapitalGainsTaxMixin

from ashare_quant.execution_six.native import Proxy, Source

from .calendar_facts import adjacent, callback_previous
from .fee_host import FeeAPI
from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod


class StateAPI(FeeAPI):
    def initialize_state(self):
        h = self._host
        h.counter = 0
        h.state_created = h.at.isoformat()
        self.emit("state_initialized", counter=h.counter, created=h.state_created)

    def callback(self, name):
        self.emit(
            "callback",
            name=name,
            state_counter=self._host.counter,
            **callback_previous(self._host),
        )

    def increment_state(self):
        h = self._host
        before = h.counter
        h.counter += 1
        self.emit("state_update", before=before, after=h.counter, state_created=h.state_created)

    def order(self, *args, **kwargs):
        raise RuntimeError("STATE_ONLY_ORDER_DENIED")


class StateProxy(Proxy):
    def period(self, caller, field):
        ledger = caller.f_locals["self"]
        if (
            ledger._annual_deductible_balance != 0
            or ledger._monthly_realized_pnl != 0
            or ledger._env.config.base.capital_gain_tax_rate != 0
        ):
            raise RuntimeError("STATE_ONLY_NONZERO_TAX_BASIS_DENIED")
        h = self._data_source.host
        h.require_empty()
        value = getattr(h.at.date(), field)
        h.emit("empty_account_inert_tax_period", field=field, value=value, adjacent_date=None)
        return SimpleNamespace(**{field: value})

    def get_previous_trading_date(self, *args, **kwargs):
        h = self._data_source.host
        if "calendar_links" in h.metadata:
            return adjacent(h, args[0], "previous_session", kwargs.get("n", 1))
        caller = sys._getframe(1)
        if caller.f_code is Executor._ensure_before_trading.__code__:
            h = self._data_source.host
            h.require_empty()
            anchor = caller.f_locals["self"]._last_before_trading
            current = caller.f_locals["event"].trading_dt.date()
            if (str(anchor), str(current)) != tuple(h.metadata["calendar"]):
                raise RuntimeError("STATE_ONLY_SETTLEMENT_ANCHOR_OUT_OF_SCOPE")
            h.emit(
                "empty_account_settlement_anchor",
                prior_processed_session=str(anchor),
                exchange_adjacency=None,
                account_has_positions=False,
            )
            return datetime.combine(anchor, time())
        if (
            caller.f_code is Account.__init__.__code__
            and caller.f_locals.get("init_positions") == {}
        ):
            self._data_source.host.emit(
                "unused_previous_session", value=None, native_consumer="empty_Account.__init__"
            )
            return None
        if caller.f_code is CapitalGainsTaxMixin._is_start_of_year.__code__:
            return self.period(caller, "year")
        raise RuntimeError("STATE_ONLY_PREVIOUS_SESSION_UNKNOWN")

    def get_next_trading_date(self, *args, **kwargs):
        h = self._data_source.host
        if "calendar_links" in h.metadata:
            return adjacent(h, args[0], "next_session", kwargs.get("n", 1))
        caller = sys._getframe(1)
        if caller.f_code is CapitalGainsTaxMixin._is_end_of_month.__code__:
            return self.period(caller, "month")
        raise RuntimeError("STATE_ONLY_NEXT_SESSION_UNKNOWN")


class StateSource(Source):
    def available_data_range(self, frequency):
        if frequency != "1m":
            raise RuntimeError("STATE_ONLY_MINUTE_CALLBACKS")
        return self.host.case.day, date.fromisoformat(self.host.case.end_day)


class Host(FeeHost):
    proxy_class = StateProxy

    def __init__(self, *args):
        super().__init__(*args)
        self.api = StateAPI(self)

    def require_empty(self):
        env = self.env
        portfolio = getattr(env, "portfolio", None)
        if portfolio is None:
            return
        a = portfolio.stock_account
        if (
            any(p.quantity for p in a.get_positions())
            or self.submissions
            or self.orders
            or self.trades
            or a.frozen_cash != 0
            or a.total_cash != float(self.case.initial_cash)
        ):
            raise RuntimeError("STATE_ONLY_ACCOUNT_NOT_EMPTY_OR_UNCHANGED")

    def before(self):
        self.require_empty()
        self.api.callback("before_trading_start")
        super().before()

    def mark_to_close(self, event):
        self.require_empty()
        self.emit("empty_account_no_valuation_required", market_price=None)

    def after(self):
        self.require_empty()
        self.api.callback("after_trading_end")
        self.snapshot("end_of_day")

    def complete(self):
        self.require_empty()
        self.api.callback("on_strategy_end")
        result = super().complete()
        success = self.counter == 2 and not self.submissions and not self.orders and not self.trades
        result.update(
            scenario_triggered=success,
            status="SCENARIO_EXECUTED" if success else "SCENARIO_NOT_TRIGGERED",
            state_counter=self.counter,
            scope="no_order_state_lifecycle_only",
            calendar_adjacency=self.metadata.get("calendar_links"),
            corporate_action_coverage=None,
            holdings_overnight_admitted=False,
        )
        return result


class Mod(FeeMod):
    source_type = StateSource


def load_mod():
    return Mod()
