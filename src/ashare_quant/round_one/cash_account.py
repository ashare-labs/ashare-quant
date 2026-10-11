"""AQ settlement restrictions inside one native RQAlpha stock account.

The native superclass is the only owner of cash, reservations and positions.
This extension stores restrictions on existing cash, never another cash balance.
Model choices are explicit in cash-model.json; JQ equivalence is unverified.
"""

from datetime import date
from decimal import Decimal
from math import ulp

from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION, POSITION_EFFECT, SIDE
from rqalpha.core.events import EVENT
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition
from rqalpha.portfolio import Portfolio
from rqalpha.portfolio.account import Account

MODEL = "aq.stock-cash-settlement.v1"


def money(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise RuntimeError("CASH_NONFINITE_AMOUNT")
    return value


class CashAccount(Account):
    """Long-only CS account; uses native trade application exactly once."""

    def __init__(self, account_type, total_cash, init_positions, financing_rate, env):
        if account_type != "STOCK" or init_positions or money(total_cash) < 0:
            raise RuntimeError("CASH_MODEL_REQUIRES_EMPTY_INITIAL_STOCK_ACCOUNT")
        if (
            not StockPosition.t_plus_enabled
            or StockPosition.dividend_reinvestment
            or StockPosition.dividend_tax_enabled
        ):
            raise RuntimeError("CASH_MODEL_REQUIRES_T1_NO_REINVESTMENT_GROSS_DIVIDEND_MODEL")
        self._aq_pending = []
        self._aq_applied = {}
        self._aq_opened = None
        self._aq_settled = None
        super().__init__(account_type, total_cash, init_positions, financing_rate, env)

    @property
    def unsettled_sale_cash(self):
        return sum((money(x["remaining"]) for x in self._aq_pending), Decimal(0))

    @property
    def transferable_cash(self):
        # Reserve unsettled proceeds first. Reservations never consume a receipt;
        # cancel/reject therefore releases the native freeze without extra posting.
        return max(
            Decimal(0),
            min(money(self.total_cash) - self.unsettled_sale_cash, money(self.cash)),
        )

    def cash_projection(self):
        return dict(
            model=MODEL,
            total_cash=str(money(self.total_cash)),
            available_cash=str(money(self.cash)),
            frozen_cash=str(money(self.frozen_cash)),
            transferable_cash=str(self.transferable_cash),
            unsettled_sale_cash=str(self.unsettled_sale_cash),
            withdrawal_execution="virtual_account_only",
            jq_equivalence="NOT_VERIFIED",
        )

    def _consume_pending(self, amount):
        """AQ model: reuse unspent sale proceeds first, in receipt order."""
        for entry in self._aq_pending:
            consumed = min(amount, money(entry["remaining"]))
            entry["remaining"] = str(money(entry["remaining"]) - consumed)
            amount -= consumed
        self._aq_pending = [x for x in self._aq_pending if money(x["remaining"]) > 0]

    def _next_session(self, current):
        value = self._env.data_proxy.get_next_trading_date(current)
        following = value.date() if hasattr(value, "date") else value
        if type(following) is not date or following <= current:
            raise RuntimeError("CASH_NEXT_SESSION_NOT_ADMITTED")
        return following

    def _unreserved_freeze_residual(self):
        """Only identify a float-sized orphan reservation; never forgive real cash.

        The absolute ceiling is one ten-millionth of a cent and the relative
        ceiling is eight binary ULPs of native cash. A live order always wins.
        """
        frozen = self.frozen_cash
        if not (0 < abs(frozen) <= min(8 * ulp(self.total_cash), 1e-9)):
            return 0.0
        broker = getattr(self._env, "broker", None)
        if broker is None or broker.get_open_orders():
            return 0.0
        return frozen

    @staticmethod
    def _trade_identity(trade):
        return dict(
            security=trade.order_book_id,
            at=trade.trading_datetime.isoformat(),
            side=trade.side.name,
            effect=trade.position_effect.name,
            quantity=str(trade.last_quantity),
            price=str(money(trade.last_price)),
            cost=str(money(trade.transaction_cost)),
            order_id=str(trade.order_id),
        )

    def apply_trade(self, trade, order=None):
        identity = self._trade_identity(trade)
        key = str(trade.exec_id)
        if key in self._aq_applied:
            if self._aq_applied[key] != identity:
                raise RuntimeError("CASH_EXECUTION_ID_COLLISION")
            return 0
        today = self._env.trading_dt.date()
        if (
            self._aq_opened != today.isoformat()
            or self._aq_settled == today.isoformat()
            or trade.trading_datetime.date() != today
        ):
            raise RuntimeError("CASH_TRADE_OUTSIDE_OPEN_SESSION")
        instrument = self._env.data_proxy.instrument_not_none(trade.order_book_id)
        if (
            instrument.type != INSTRUMENT_TYPE.CS
            or instrument.exchange not in {"XSHG", "XSHE"}
            or instrument.market_tplus != 1
            or trade.position_direction != POSITION_DIRECTION.LONG
            or (trade.side, trade.position_effect)
            not in {(SIDE.BUY, POSITION_EFFECT.OPEN), (SIDE.SELL, POSITION_EFFECT.CLOSE)}
        ):
            raise RuntimeError("CASH_INSTRUMENT_OR_TRADE_OUT_OF_SCOPE")
        # Refuse missing/clamped calendar before native cash or position mutation.
        release = self._next_session(today) if trade.side == SIDE.SELL else None
        before = money(self.total_cash)
        result = super().apply_trade(trade, order)
        delta = money(self.total_cash) - before
        if trade.side == SIDE.SELL and delta > 0:
            self._aq_pending.append(
                dict(execution_id=key, release_session=release.isoformat(), remaining=str(delta))
            )
        elif delta < 0:
            self._consume_pending(-delta)
        self._aq_applied[key] = identity
        return result

    def _on_before_trading(self, event):
        today = self._env.trading_dt.date()
        if self._aq_opened == today.isoformat():
            return  # No repeated native dividend posting or T+1 unlock.
        if self._aq_opened is not None:
            previous = date.fromisoformat(self._aq_opened)
            if self._aq_settled != self._aq_opened or self._next_session(previous) != today:
                raise RuntimeError("CASH_NONADJACENT_OR_UNSETTLED_SESSION")
        super()._on_before_trading(event)
        self._aq_pending = [
            x for x in self._aq_pending if date.fromisoformat(x["release_session"]) > today
        ]
        if self._unreserved_freeze_residual():
            self._frozen_cash = 0.0
        self._aq_opened = today.isoformat()

    def _on_settlement(self, event):
        today = self._env.trading_dt.date().isoformat()
        if self._aq_settled == today:
            return
        if self._aq_opened != today:
            raise RuntimeError("CASH_SETTLEMENT_WITHOUT_OPEN_SESSION")
        before = money(self.total_cash)
        super()._on_settlement(event)
        delta = money(self.total_cash) - before
        if delta > 0:
            # Delisting cash or other unsupported settlement income must never
            # quietly become transferable; the enclosing run must abort.
            raise RuntimeError("CASH_UNSUPPORTED_SETTLEMENT_CREDIT")
        self._consume_pending(-delta)
        self._aq_settled = today

    def deposit_withdraw(self, amount, receiving_days=0):
        amount = money(amount)
        if receiving_days != 0:
            raise RuntimeError("CASH_DELAYED_TRANSFER_OUT_OF_SCOPE")
        residual = self._unreserved_freeze_residual()
        limit = self.transferable_cash
        if residual:
            limit = max(
                Decimal(0),
                min(
                    money(self.total_cash) - self.unsettled_sale_cash,
                    money(self.total_cash - self.margin),
                ),
            )
        if amount < 0 and -amount > limit:
            raise ValueError("CASH_WITHDRAWAL_EXCEEDS_TRANSFERABLE")
        if residual:
            self._frozen_cash = 0.0
        # Virtual transfer only; native Account still applies the sole cash delta.
        super().deposit_withdraw(float(amount), receiving_days=0)

    def finance_repay(self, amount):
        raise RuntimeError("CASH_FINANCING_OUT_OF_SCOPE")

    def pay_taxes(self, amount, tax_type):
        raise RuntimeError("CASH_ADDITIONAL_ACCOUNT_TAX_MODEL_NOT_ADMITTED")

    def get_state(self):
        state = super().get_state()
        state["aq_cash"] = dict(
            model=MODEL,
            pending=[dict(x) for x in self._aq_pending],
            applied={k: dict(v) for k, v in self._aq_applied.items()},
            opened=self._aq_opened,
            settled=self._aq_settled,
        )
        return state

    def set_state(self, state):
        value = state.get("aq_cash")
        if not isinstance(value, dict) or value.get("model") != MODEL:
            raise RuntimeError("CASH_CHECKPOINT_MODEL_REQUIRED")
        opened, settled = value["opened"], value["settled"]
        for day in (opened, settled):
            if day is not None:
                date.fromisoformat(day)
        if settled is not None and (opened is None or settled > opened):
            raise RuntimeError("CASH_CHECKPOINT_PHASE_INVALID")
        for entry in value["pending"]:
            if (
                opened is None
                or date.fromisoformat(entry["release_session"]) <= date.fromisoformat(opened)
                or money(entry["remaining"]) <= 0
                or entry["execution_id"] not in value["applied"]
            ):
                raise RuntimeError("CASH_CHECKPOINT_PENDING_INVALID")
        super().set_state(state)
        self._aq_pending = [dict(x) for x in value["pending"]]
        self._aq_applied = {k: dict(v) for k, v in value["applied"].items()}
        self._aq_opened, self._aq_settled = opened, settled


class CashPortfolio(Portfolio):
    @classmethod
    def _init_accounts(cls, starting_cash, init_positions, financing_rate, env):
        if set(starting_cash) != {"STOCK"} or init_positions:
            raise RuntimeError("CASH_PORTFOLIO_SCOPE_UNSUPPORTED")
        return {"STOCK": CashAccount("STOCK", starting_cash["STOCK"], {}, financing_rate, env)}


def install_cash_account(env):
    """Use RQAlpha's INIT_PORTFOLIO seam before any account exists."""

    def initialize(_):
        if hasattr(env, "portfolio"):
            raise RuntimeError("CASH_PORTFOLIO_ALREADY_EXISTS")
        env.set_portfolio(
            CashPortfolio(
                env.config.base.accounts,
                env.config.base.init_positions,
                env.config.mod.sys_accounts.financing_rate,
                env,
            )
        )

    env.event_bus.add_listener(EVENT.INIT_PORTFOLIO, initialize)
