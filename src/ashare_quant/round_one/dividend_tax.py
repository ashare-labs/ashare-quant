"""Opt-in JQ simulation withholding at RQAlpha's native payable-return seam.

No cash balance, entitlement quantity, cost adjustment or duplicate dividend
queue is maintained here. Native StockPosition drains its own receivable; its
one returned net delta is applied once by the existing native CashAccount.
"""

from decimal import Decimal

MODEL = "aq.jq-fixed-dividend-tax.v1"
RATE = Decimal("0.20")


def net_payable(gross):
    value = Decimal(str(gross))
    if not value.is_finite() or value < 0:
        raise RuntimeError("JQ_DIVIDEND_PAYABLE_INVALID")
    return float(value * (1 - RATE))


def install_fixed_tax_position(env, policy):
    from rqalpha.const import INSTRUMENT_TYPE
    from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition
    from rqalpha.portfolio.position import POSITION_TYPE_MAP

    if (
        policy.get("schema") != MODEL
        or policy.get("rate") != str(RATE)
        or hasattr(env, "portfolio")
        or POSITION_TYPE_MAP.get(INSTRUMENT_TYPE.CS) is not StockPosition
        or StockPosition.dividend_reinvestment
        or StockPosition.dividend_tax_enabled
    ):
        raise RuntimeError("JQ_DIVIDEND_POSITION_INSTALLATION_REFUSED")

    class FixedTaxStockPosition(StockPosition):
        __instrument_types__ = [INSTRUMENT_TYPE.CS]

        def _handle_dividend_payable(self, trading_date):
            if self.dividend_reinvestment or self.dividend_tax_enabled:
                raise RuntimeError("JQ_DIVIDEND_CONFLICTING_NATIVE_POLICY")
            return net_payable(super()._handle_dividend_payable(trading_date))

        def get_state(self):
            state = super().get_state()
            state["aq_dividend_tax"] = dict(model=MODEL, rate=str(RATE))
            return state

        def set_state(self, state):
            if state.get("aq_dividend_tax") != dict(model=MODEL, rate=str(RATE)):
                raise RuntimeError("JQ_DIVIDEND_CHECKPOINT_MODEL_MISMATCH")
            super().set_state(state)

    return FixedTaxStockPosition
