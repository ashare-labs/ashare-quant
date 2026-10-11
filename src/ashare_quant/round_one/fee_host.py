"""Fee case facade, using only native broker/account/matcher mutations."""

import importlib

from rqalpha.const import POSITION_DIRECTION

from ashare_quant.execution_six.host import Host as SixHost
from ashare_quant.execution_six.host import Mod as SixMod
from ashare_quant.execution_six.host import StrategyAPI


class FeeAPI(StrategyAPI):
    @property
    def transferable_cash(self):
        from .cash_account import CashAccount

        account = self._host.env.portfolio.stock_account
        if type(account) is not CashAccount:
            raise RuntimeError("TRANSFERABLE_CASH_MODEL_NOT_ENABLED")
        return account.transferable_cash

    @property
    def case(self):
        return self._host.case

    @property
    def day(self):
        return self._host.at.date()

    @property
    def sellable(self):
        return self._host.env.portfolio.get_position(
            self.case.security, POSITION_DIRECTION.LONG
        ).closable

    def order(self, quantity):
        from rqalpha.api import order_shares
        from rqalpha.model.order import MarketOrder

        h = self._host
        buy = self.day == self.case.day
        allowed = self.case.buy_orders if buy else 1
        if (
            self.clock != self.case.submit_at
            or quantity != (self.case.quantity if buy else -self.case.quantity)
            or h.daily_submissions >= allowed
        ):
            raise RuntimeError("ROUND_ONE_ORDER_OUTSIDE_FROZEN_INTENT")
        h.daily_submissions += 1
        h.submissions += 1
        h.emit("order_request", quantity=quantity, style="market", limit_price=None)
        order = order_shares(self.case.security, quantity, price_or_style=MarketOrder())
        if order is not None:
            h.orders[str(order.order_id)] = order
        h.emit(
            "order_return",
            returned_none=order is None,
            order=None if order is None else h.project_order(order),
        )
        return order


class Host(SixHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.api = FeeAPI(self)
        self.daily_submissions = 0

    def initialize(self):
        module = importlib.import_module(
            "ashare_quant.round_one." + self.case.strategy_path_name.removesuffix(".py")
        )
        module.initialize(self.api)
        self.started = True
        self.emit("strategy_initialized", callbacks=list(self.callbacks))

    def before(self):
        self.daily_submissions = 0
        self.snapshot("day_start")

    def project_order(self, order):
        return {**super().project_order(order), "side": order.side.name}

    def account(self):
        from .cash_account import CashAccount

        value = super().account()
        account = self.env.portfolio.stock_account
        if type(account) is CashAccount:
            value.update(
                transferable_cash=str(account.transferable_cash),
                cash_settlement=account.cash_projection(),
            )
        return value

    def complete(self):
        self.snapshot("strategy_end")
        self.emit("strategy_end")
        expected = self.case.buy_orders + (self.case.end_day != str(self.case.day))
        triggered = (
            self.submissions == expected
            and len(self.orders) == expected
            and all(o.filled_quantity == o.quantity for o in self.orders.values())
            and not any(e["kind"] == "not_triggered" for e in self.records)
        )
        return dict(
            case_id=self.case.case_id,
            scenario_triggered=triggered,
            status="SCENARIO_EXECUTED" if triggered else "SCENARIO_NOT_TRIGGERED",
            submissions=self.submissions,
            orders=[self.project_order(o) for o in self.orders.values()],
            trades=self.trades,
            snapshots=[e for e in self.records if e["kind"] == "snapshot"],
            final_account=self.account(),
            owner_reads=self.reads,
            event_count=len(self.records),
            cross_platform_comparison="NOT_PERFORMED",
            native_account_owner=type(self.env.portfolio.stock_account).__module__
            + "."
            + type(self.env.portfolio.stock_account).__name__,
        )


class Mod(SixMod):
    def start_up(self, env, config):
        super().start_up(env, config)
        model = getattr(config, "cash_model", None)
        if model is not None:
            from .cash_account import MODEL, install_cash_account

            if model != MODEL:
                raise RuntimeError("CASH_EXECUTION_MODEL_UNKNOWN")
            install_cash_account(env)


def load_mod():
    return Mod()
