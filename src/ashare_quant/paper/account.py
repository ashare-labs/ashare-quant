"""Opt-in native budget state serialization; legacy B44 admission is unchanged."""

from copy import deepcopy

from rqalpha.core.events import EVENT
from rqalpha.portfolio import Portfolio
from rqalpha.portfolio.account import Account

from ashare_quant.second_batch.reservation import BudgetAccount, number


class RestorableBudgetAccount(BudgetAccount):
    def get_state(self):
        return {
            "schema": "paper.native-budget-state.v1",
            "native": super().get_state(),
            # Portfolio's pinned jsonpickle encoding stringifies dict keys.
            # Pair sequences retain native integer identities without coercion.
            "pending": deepcopy(list(self._budget_pending.items())),
            "terminal": deepcopy(list(self._budget_terminal.items())),
            "trades": deepcopy(list(self._budget_trades.items())),
        }

    def set_state(self, state):
        if set(state) != {"schema", "native", "pending", "terminal", "trades"} or (
            state["schema"] != "paper.native-budget-state.v1"
        ):
            raise RuntimeError("PAPER_BUDGET_STATE_SCHEMA")
        candidate = deepcopy(state)
        for key, width in (("pending", 7), ("terminal", 3), ("trades", 7)):
            values = candidate[key]
            if not isinstance(values, list) or any(
                type(i) is not int or not isinstance(v, tuple) or len(v) != width for i, v in values
            ):
                raise RuntimeError("PAPER_BUDGET_STATE_INVALID")
            candidate[key] = dict(values)
            if len(candidate[key]) != len(values):
                raise RuntimeError("PAPER_BUDGET_DUPLICATE_ID")
        if not set(candidate["terminal"]) <= set(candidate["pending"]) or any(
            t[0] not in candidate["pending"] for t in candidate["trades"].values()
        ):
            raise RuntimeError("PAPER_BUDGET_STATE_LINK")
        native = candidate["native"]
        if number(native["frozen_cash"]) < 0 or number(native["total_cash"]) < 0:
            raise RuntimeError("PAPER_BUDGET_STATE_CASH")
        # Account owns the entire native book. No accounting is recreated here.
        Account.set_state(self, native)
        self._budget_pending = candidate["pending"]
        self._budget_terminal = candidate["terminal"]
        self._budget_trades = candidate["trades"]

    def fast_forward(self, orders=None, trades=None):
        if trades:
            raise RuntimeError("PAPER_EXTERNAL_TRADE_REPLAY_UNSUPPORTED")
        if orders is not None:
            active = [o for o in orders if o.is_active()]
            for order in active:
                self._bound(order)
            expected = sum(self._remaining_frozen_cash_of_order(o) for o in active)
            if abs(expected - self.frozen_cash) > 1e-8:
                raise RuntimeError("PAPER_RESTORED_RESERVATION_MISMATCH")


class DeclaredInventoryAccount(RestorableBudgetAccount):
    """Native starting positions, separately declared rather than fabricated trades."""

    def __init__(self, account_type, total_cash, init_positions, financing_rate, env):
        self._budget_pending, self._budget_terminal, self._budget_trades = {}, {}, {}
        Account.__init__(self, account_type, total_cash, init_positions, financing_rate, env)


class NativePaperPortfolio(Portfolio):
    @classmethod
    def _init_accounts(cls, starting_cash, init_positions, financing_rate, env):
        if set(starting_cash) != {"STOCK"} or financing_rate:
            raise RuntimeError("PAPER_STOCK_GENESIS_ONLY")
        account_class = DeclaredInventoryAccount if init_positions else RestorableBudgetAccount
        return {"STOCK": account_class("STOCK", starting_cash["STOCK"], init_positions, 0, env)}


def install(env, initial_inventory=None):
    def initialize(_):
        if hasattr(env, "portfolio"):
            raise RuntimeError("PAPER_NATIVE_PORTFOLIO_ALREADY_EXISTS")
        env.set_portfolio(
            NativePaperPortfolio(env.config.base.accounts, initial_inventory or {}, 0, env)
        )

    env.event_bus.add_listener(EVENT.INIT_PORTFOLIO, initialize)
