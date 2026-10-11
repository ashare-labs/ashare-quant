"""Opt-in order-budget freezing inside the native stock account.

Cash, positions and cumulative costs are still updated by Account.apply_trade.
Only the reservation changes: a live buy limit retains its original notional
less actual fills and fees. Other orders retain the native freeze convention.
This bounded model supports fresh sessions, not checkpoint restoration.
"""

from decimal import Decimal

from rqalpha.const import ORDER_TYPE, POSITION_EFFECT, SIDE
from rqalpha.core.events import EVENT
from rqalpha.portfolio import Portfolio
from rqalpha.portfolio.account import Account

MODEL = "aq.native-buy-limit-budget-reservation.v1"


def number(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise RuntimeError("BUDGET_NONFINITE")
    return value


def budget_remaining(quantity, limit, filled, average, cost):
    if (
        type(quantity) is not int
        or type(filled) is not int
        or not 0 <= filled <= quantity
        or quantity <= 0
        or number(limit) <= 0
        or number(average) < 0
        or number(cost) < 0
    ):
        raise RuntimeError("BUDGET_ORDER_STATE")
    if filled == quantity:
        return Decimal(0)
    return max(Decimal(0), quantity * number(limit) - filled * number(average) - number(cost))


class BudgetAccount(Account):
    def __init__(self, account_type, total_cash, init_positions, financing_rate, env):
        if account_type != "STOCK" or init_positions or financing_rate:
            raise RuntimeError("BUDGET_EMPTY_STOCK_ACCOUNT_REQUIRED")
        self._budget_pending = {}
        self._budget_terminal = {}
        self._budget_trades = {}
        super().__init__(account_type, total_cash, init_positions, financing_rate, env)

    @staticmethod
    def _budget(order):
        return (order.type, order.side, order.position_effect) == (
            ORDER_TYPE.LIMIT,
            SIDE.BUY,
            POSITION_EFFECT.OPEN,
        )

    @staticmethod
    def _identity(order):
        return (
            order.order_book_id,
            order.quantity,
            str(order.frozen_price),
            str(order.type),
            str(order.side),
            str(order.position_effect),
            order.trading_datetime.isoformat(),
        )

    def _bound(self, order):
        if self._budget_pending.get(order.order_id) != self._identity(order):
            raise RuntimeError("BUDGET_ORDER_IDENTITY")

    def _frozen_cash_of_order(self, order):
        if self._budget(order):
            return float(number(order.quantity) * number(order.frozen_price))
        return super()._frozen_cash_of_order(order)

    def _remaining_frozen_cash_of_order(self, order):
        if order.order_id in self._budget_terminal:
            return 0.0
        if self._budget(order):
            return float(
                budget_remaining(
                    order.quantity,
                    order.frozen_price,
                    order.filled_quantity,
                    order.avg_price,
                    order.transaction_cost,
                )
            )
        return super()._remaining_frozen_cash_of_order(order)

    def _on_order_pending_new(self, event):
        if event.account is not self:
            return
        order = event.order
        if order.order_id in self._budget_pending:
            self._bound(order)
            return
        if order.filled_quantity:
            raise RuntimeError("BUDGET_NEW_ORDER_ALREADY_FILLED")
        self._budget_pending[order.order_id] = self._identity(order)
        super()._on_order_pending_new(event)

    def apply_trade(self, trade, order=None):
        identity = (
            trade.order_id,
            trade.order_book_id,
            trade.last_quantity,
            str(trade.last_price),
            str(trade.transaction_cost),
            str(trade.side),
            trade.trading_datetime.isoformat(),
        )
        if trade.exec_id in self._budget_trades:
            if self._budget_trades[trade.exec_id] != identity:
                raise RuntimeError("BUDGET_TRADE_ID_COLLISION")
            return 0
        if order is None:
            raise RuntimeError("BUDGET_NATIVE_ORDER_CONTEXT_REQUIRED")
        self._bound(order)
        if trade.order_id != order.order_id or order.order_id in self._budget_terminal:
            raise RuntimeError("BUDGET_TRADE_PHASE")
        frozen_before = self._frozen_cash
        release = None
        if self._budget(order):
            total = number(order.quantity) * number(order.frozen_price)
            spent = number(order.filled_quantity) * number(order.avg_price) + number(
                order.transaction_cost
            )
            cost = number(trade.last_quantity) * number(trade.last_price) + number(
                trade.transaction_cost
            )
            before = max(Decimal(0), total - (spent - cost))
            after = number(self._remaining_frozen_cash_of_order(order))
            release = before - after
        result = super().apply_trade(trade, order)
        if release is not None:
            # Replace only this trade's native proportional release. Other live
            # reservations remain in frozen_before, and native total cash is untouched.
            self._frozen_cash = frozen_before - float(release)
        self._budget_trades[trade.exec_id] = identity
        if self.cash < -1e-8 or self.frozen_cash < -1e-8:
            raise RuntimeError("BUDGET_NEGATIVE_CASH")
        return result

    def _on_order_unsolicited_update(self, event):
        if event.account is not self:
            return
        order = event.order
        self._bound(order)
        state = (str(order.status), order.filled_quantity, str(order.transaction_cost))
        if order.order_id in self._budget_terminal:
            if self._budget_terminal[order.order_id] != state:
                raise RuntimeError("BUDGET_TERMINAL_REPLAY_CONFLICT")
            return
        self._frozen_cash -= self._remaining_frozen_cash_of_order(order)
        self._budget_terminal[order.order_id] = state
        if self.frozen_cash < -1e-8:
            raise RuntimeError("BUDGET_NEGATIVE_FREEZE")

    def fast_forward(self, orders=None, trades=None):
        if orders is not None or trades is not None:
            raise RuntimeError("BUDGET_CHECKPOINT_RESTORE_UNSUPPORTED")

    def set_state(self, state):
        raise RuntimeError("BUDGET_CHECKPOINT_RESTORE_UNSUPPORTED")


class BudgetPortfolio(Portfolio):
    @classmethod
    def _init_accounts(cls, starting_cash, init_positions, financing_rate, env):
        if set(starting_cash) != {"STOCK"} or init_positions:
            raise RuntimeError("BUDGET_STOCK_ONLY")
        return {"STOCK": BudgetAccount("STOCK", starting_cash["STOCK"], {}, financing_rate, env)}


def install_budget_account(env):
    if hasattr(env, "portfolio"):
        raise RuntimeError("BUDGET_PORTFOLIO_ALREADY_EXISTS")

    def initialize(_):
        if hasattr(env, "portfolio"):
            raise RuntimeError("BUDGET_PORTFOLIO_ALREADY_EXISTS")
        env.set_portfolio(
            BudgetPortfolio(
                env.config.base.accounts,
                env.config.base.init_positions,
                env.config.mod.sys_accounts.financing_rate,
                env,
            )
        )

    env.event_bus.add_listener(EVENT.INIT_PORTFOLIO, initialize)
