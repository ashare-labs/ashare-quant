"""Opt-in zero-cost buy-limit reservation policy inside one native account.

Only one same-session order, one partial fill, then user cancellation. Native
trade application still owns total cash and positions. Stored order/trade
identities are replay guards, not another cash balance.
"""

import copy
from datetime import date
from decimal import Decimal
from math import ulp

from rqalpha.const import (
    INSTRUMENT_TYPE,
    ORDER_STATUS,
    ORDER_TYPE,
    POSITION_DIRECTION,
    POSITION_EFFECT,
    SIDE,
)
from rqalpha.core.events import EVENT
from rqalpha.interface import TransactionCostArgs
from rqalpha.portfolio import Portfolio
from rqalpha.portfolio.account import Account

MODEL = "aq.zero-cost-partial-limit-reservation.v1"


def amount(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise RuntimeError("RESERVATION_NONFINITE")
    return value


def remaining(record):
    if record is None or record["cancelled"]:
        return Decimal(0)
    total = amount(record["order"]["quantity"]) * amount(record["order"]["limit"])
    trade = record["trade"]
    return total if trade is None else total - amount(trade["quantity"]) * amount(trade["price"])


def same_money(left, right):
    # Validate native float representation; never forgive a cent-sized difference.
    return abs(float(left) - float(right)) <= min(8 * ulp(float(right)), 1e-9)


class ReservationAccount(Account):
    def __init__(self, account_type, total_cash, init_positions, financing_rate, env):
        if account_type != "STOCK" or init_positions or financing_rate or amount(total_cash) < 0:
            raise RuntimeError("RESERVATION_EMPTY_CASH_ACCOUNT_REQUIRED")
        if getattr(env.config.base, "partial_fill_on_insufficient_cash", False):
            raise RuntimeError("RESERVATION_NO_PARTIAL_CASH_ADMISSION")
        self._reservation = None
        super().__init__(account_type, total_cash, init_positions, financing_rate, env)

    def _order_identity(self, order):
        # Native broker restores order fields without its transient _env cache.
        # Resolve metadata/costs through this account's bound native environment.
        instrument = self._env.data_proxy.instrument_not_none(order.order_book_id)
        estimated_cost = self._env.calc_transaction_cost(
            TransactionCostArgs(
                instrument, order.frozen_price, order.quantity, order.side, order.position_effect
            )
        ).total
        if (
            (
                instrument.type,
                order.side,
                order.position_effect,
                order.position_direction,
                order.type,
            )
            != (
                INSTRUMENT_TYPE.CS,
                SIDE.BUY,
                POSITION_EFFECT.OPEN,
                POSITION_DIRECTION.LONG,
                ORDER_TYPE.LIMIT,
            )
            or amount(estimated_cost) != 0
            or instrument.exchange not in {"XSHE", "XSHG"}
        ):
            raise RuntimeError("RESERVATION_ONLY_ZERO_COST_BUY_LIMIT")
        if order.quantity <= 0 or amount(order.frozen_price) <= 0:
            raise RuntimeError("RESERVATION_ORDER_AMOUNT_INVALID")
        return dict(
            id=str(order.order_id),
            security=order.order_book_id,
            quantity=int(order.quantity),
            limit=str(amount(order.frozen_price)),
            session=str(order.trading_datetime.date()),
        )

    @staticmethod
    def _trade_identity(trade):
        if (trade.side, trade.position_effect, trade.position_direction) != (
            SIDE.BUY,
            POSITION_EFFECT.OPEN,
            POSITION_DIRECTION.LONG,
        ):
            raise RuntimeError("RESERVATION_ONLY_BUY_TRADE")
        if (
            amount(trade.transaction_cost) != 0
            or amount(trade.last_price) <= 0
            or trade.last_quantity <= 0
        ):
            raise RuntimeError("RESERVATION_ONLY_POSITIVE_ZERO_COST_TRADE")
        return dict(
            id=str(trade.exec_id),
            order_id=str(trade.order_id),
            security=trade.order_book_id,
            quantity=int(trade.last_quantity),
            price=str(amount(trade.last_price)),
            at=trade.trading_datetime.isoformat(),
        )

    def _bound_order(self, order):
        if self._reservation is None or self._order_identity(order) != self._reservation["order"]:
            raise RuntimeError("RESERVATION_ORDER_IDENTITY_MISMATCH")
        if str(self._env.trading_dt.date()) != self._reservation["order"]["session"]:
            raise RuntimeError("RESERVATION_CROSS_SESSION_UNSUPPORTED")
        expected = (
            0 if self._reservation["trade"] is None else self._reservation["trade"]["quantity"]
        )
        if order.filled_quantity != expected or amount(order.transaction_cost) != 0:
            raise RuntimeError("RESERVATION_ORDER_FILL_STATE_MISMATCH")

    def _on_order_pending_new(self, event):
        if event.account is not self:
            return
        order = event.order
        identity = self._order_identity(order)
        if str(self._env.trading_dt.date()) != identity["session"] or order.filled_quantity:
            raise RuntimeError("RESERVATION_NEW_ORDER_STATE_INVALID")
        if self._reservation is not None:
            if identity != self._reservation["order"]:
                raise RuntimeError("RESERVATION_SINGLE_ORDER_ONLY")
            self._bound_order(order)
            return
        if self.frozen_cash != 0:
            raise RuntimeError("RESERVATION_EXISTING_FREEZE_UNSUPPORTED")
        super()._on_order_pending_new(event)
        self._reservation = dict(order=identity, trade=None, cancelled=False)
        if not same_money(self.frozen_cash, remaining(self._reservation)):
            raise RuntimeError("RESERVATION_NATIVE_INITIAL_FREEZE_MISMATCH")
        self._frozen_cash = float(remaining(self._reservation))

    def apply_trade(self, trade, order=None):
        identity = self._trade_identity(trade)
        record = self._reservation
        if record is None:
            raise RuntimeError("RESERVATION_ORDER_REQUIRED")
        prior = record["trade"]
        if prior is not None:
            if identity == prior and trade.exec_id in self._backward_trade_set:
                return 0
            raise RuntimeError("RESERVATION_ONE_PARTIAL_FILL_ONLY_OR_ID_COLLISION")
        if order is None or record["cancelled"] or self._order_identity(order) != record["order"]:
            raise RuntimeError("RESERVATION_BOUND_ACTIVE_ORDER_REQUIRED")
        if (identity["order_id"], identity["security"], identity["at"][:10]) != (
            record["order"]["id"],
            record["order"]["security"],
            record["order"]["session"],
        ) or str(self._env.trading_dt.date()) != record["order"]["session"]:
            raise RuntimeError("RESERVATION_TRADE_SCOPE_MISMATCH")
        if (
            not 0 < identity["quantity"] < order.quantity
            or order.filled_quantity != identity["quantity"]
        ):
            raise RuntimeError("RESERVATION_ONE_PARTIAL_FILL_REQUIRED")
        if amount(identity["price"]) > amount(record["order"]["limit"]):
            raise RuntimeError("RESERVATION_LIMIT_PRICE_EXCEEDED")
        # Native code applies the trade exactly once, including cash and position.
        answer = super().apply_trade(trade, order)
        record["trade"] = identity
        # This single reservation remains initial order value less actual cash paid.
        # There are no other live orders whose reservations could be overwritten.
        self._frozen_cash = float(remaining(record))
        return answer

    def _remaining_frozen_cash_of_order(self, order):
        self._bound_order(order)
        return float(remaining(self._reservation))

    def _on_order_unsolicited_update(self, event):
        if event.account is not self:
            return
        self._bound_order(event.order)
        record = self._reservation
        if event.order.status != ORDER_STATUS.CANCELLED or record["trade"] is None:
            raise RuntimeError("RESERVATION_REQUIRES_CANCEL_AFTER_PARTIAL_FILL")
        if record["cancelled"]:
            return
        expected = self._remaining_frozen_cash_of_order(event.order)
        if not same_money(self.frozen_cash, expected):
            raise RuntimeError("RESERVATION_RELEASE_STATE_MISMATCH")
        self._frozen_cash -= expected
        record["cancelled"] = True

    def fast_forward(self, orders=None, trades=None):
        # Restore may replay already committed trades; unseen trades without their
        # order/event context are outside this deliberately narrow model.
        for trade in trades or []:
            self.apply_trade(trade)
        active = []
        for order in orders or []:
            self._bound_order(order)
            if order.is_active():
                if self._reservation["cancelled"]:
                    raise RuntimeError("RESERVATION_RESTORE_ORDER_PHASE_MISMATCH")
                active.append(order)
        if len(active) > 1:
            raise RuntimeError("RESERVATION_SINGLE_ORDER_ONLY")
        if orders is not None:
            expected = sum(self._remaining_frozen_cash_of_order(o) for o in active)
            if not same_money(expected, remaining(self._reservation)):
                raise RuntimeError("RESERVATION_RESTORE_ORDER_MISSING")
            self._frozen_cash = expected

    def _on_before_trading(self, event):
        if self._reservation is not None and self._reservation["order"]["session"] != str(
            self._env.trading_dt.date()
        ):
            raise RuntimeError("RESERVATION_CROSS_SESSION_UNSUPPORTED")
        return super()._on_before_trading(event)

    def _on_settlement(self, event):
        if self._reservation is not None and not self._reservation["cancelled"]:
            raise RuntimeError("RESERVATION_CANCEL_REQUIRED_BEFORE_SETTLEMENT")
        return super()._on_settlement(event)

    def get_state(self):
        return dict(
            super().get_state(),
            aq_reservation=dict(model=MODEL, record=copy.deepcopy(self._reservation)),
        )

    def set_state(self, state):
        value = state.get("aq_reservation")
        if not isinstance(value, dict) or value.get("model") != MODEL:
            raise RuntimeError("RESERVATION_CHECKPOINT_MODEL_REQUIRED")
        record = copy.deepcopy(value["record"])
        if record is not None:
            if type(record["cancelled"]) is not bool:
                raise RuntimeError("RESERVATION_CHECKPOINT_PHASE_INVALID")
            order, trade = record["order"], record["trade"]
            date.fromisoformat(order["session"])
            if (
                type(order["quantity"]) is not int
                or order["quantity"] <= 0
                or amount(order["limit"]) <= 0
            ):
                raise RuntimeError("RESERVATION_CHECKPOINT_ORDER_INVALID")
            if trade is not None:
                if (
                    (trade["order_id"], trade["security"], trade["at"][:10])
                    != (order["id"], order["security"], order["session"])
                    or not 0 < trade["quantity"] < order["quantity"]
                    or not 0 < amount(trade["price"]) <= amount(order["limit"])
                ):
                    raise RuntimeError("RESERVATION_CHECKPOINT_TRADE_INVALID")
            elif record["cancelled"]:
                raise RuntimeError("RESERVATION_CHECKPOINT_PHASE_INVALID")
        expected_ids = (
            set() if record is None or record["trade"] is None else {record["trade"]["id"]}
        )
        if {str(i) for i in state["backward_trade_set"]} != expected_ids or not same_money(
            state["frozen_cash"], remaining(record)
        ):
            raise RuntimeError("RESERVATION_CHECKPOINT_FREEZE_OR_REPLAY_MISMATCH")
        super().set_state(state)
        self._reservation = record


class ReservationPortfolio(Portfolio):
    @classmethod
    def _init_accounts(cls, starting_cash, init_positions, financing_rate, env):
        if set(starting_cash) != {"STOCK"} or init_positions:
            raise RuntimeError("RESERVATION_STOCK_ONLY_PORTFOLIO")
        return {
            "STOCK": ReservationAccount("STOCK", starting_cash["STOCK"], {}, financing_rate, env)
        }


def install_reservation_account(env, model):
    if model != MODEL:
        raise RuntimeError("RESERVATION_MODEL_UNKNOWN")
    costs = env.config.mod.sys_transaction_cost
    if any(
        getattr(costs, k) != 0
        for k in ("stock_min_commission", "stock_commission_multiplier", "tax_multiplier")
    ):
        raise RuntimeError("RESERVATION_ZERO_COST_CONFIG_REQUIRED")
    if hasattr(env, "portfolio"):
        raise RuntimeError("RESERVATION_PORTFOLIO_ALREADY_EXISTS")

    def initialize(_):
        if hasattr(env, "portfolio"):
            raise RuntimeError("RESERVATION_PORTFOLIO_ALREADY_EXISTS")
        env.set_portfolio(
            ReservationPortfolio(
                env.config.base.accounts,
                env.config.base.init_positions,
                env.config.mod.sys_accounts.financing_rate,
                env,
            )
        )

    env.event_bus.add_listener(EVENT.INIT_PORTFOLIO, initialize)
