"""Date-level entitlements inside the native stock position/checkpoint.

Account remains the sole cash owner: position.before_trading returns its cash
delta to native Account. Credits are inventory changes, never fabricated fills.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction

from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition
from rqalpha.portfolio.position import POSITION_TYPE_MAP, Position

from ashare_quant.core.validation import digest, require


def install(host, *, on_stage=None):
    """Install account rules; a checkpoint owner may supply its own fault probe.

    Corporate settlement also runs under non-resumable Python clock sources.
    It must not require their event source to implement a checkpoint cursor.
    """
    class CorporateStockPosition(StockPosition):
        __instrument_types__ = []  # Install only in this frozen native subprocess.

        def __init__(self, instrument, direction, init_quantity=0, init_price=None):
            super().__init__(instrument, direction, int(init_quantity), init_price)
            self.entitlements = {}
            self._pending_begin = self._new_today = self._credited_today = 0

        def __bool__(self):
            # Native Account normally drops empty positions before callbacks. A
            # zero-quantity record may still owe cash/shares or carry dedup keys.
            return bool(self.entitlements) or super().__bool__()

        def mark(self, entry, stage):
            key = entry["event"]["settlement_keys"].get(
                stage, entry["event"]["entitlement_key"] + ":ex"
            )
            require(key not in entry["stages"], "CORPORATE_STAGE_ALREADY_APPLIED")
            entry["stages"][key] = host.day
            if on_stage is not None:
                on_stage("corporate_" + stage)

        def done(self, entry, stage):
            key = entry["event"]["settlement_keys"].get(
                stage, entry["event"]["entitlement_key"] + ":ex"
            )
            return key in entry["stages"]

        def record(self, event):
            key = event["entitlement_key"]
            if key in self.entitlements:
                require(
                    self.entitlements[key]["source_sha256"] == digest(event),
                    "CORPORATE_REVISION_RECONCILIATION_REQUIRED",
                )
                return
            # Exact entitlement integrality must not depend on a Decimal context
            # rounding a very small fractional remainder away.
            shares = self.quantity * (
                Fraction(event["bonus_share_ratio"]) + Fraction(event["capitalization_share_ratio"])
            )
            require(shares.denominator == 1, "CORPORATE_FRACTIONAL_ENTITLEMENT_UNSUPPORTED")
            coefficient = Decimal(event["cash_per_share"])
            precision = max(50, len(coefficient.as_tuple().digits) + len(str(self.quantity)))
            with localcontext(prec=precision):
                entry = dict(
                    event=deepcopy(event),
                    source_sha256=digest(event),
                    basis=self.quantity,
                    cash=str(self.quantity * coefficient),
                    shares=int(shares),
                    pending_cost="0",
                    stages={},
                )
            self.entitlements[key] = entry
            self.mark(entry, "record")

        @property
        def dividend_receivable(self):
            return sum(
                float(e["cash"])
                for e in self.entitlements.values()
                if self.done(e, "ex") and not self.done(e, "cash")
            )

        @property
        def pending_stock(self):
            return sum(
                e["shares"]
                for e in self.entitlements.values()
                if self.done(e, "ex") and not self.done(e, "stock_credit")
            )

        @property
        def restricted_stock(self):
            return sum(
                e["shares"]
                for e in self.entitlements.values()
                if self.done(e, "stock_credit") and not self.done(e, "stock_tradable")
            )

        @property
        def equity(self):
            return super().equity + (
                self.pending_stock * self.last_price if self.pending_stock else 0
            )

        @property
        def closable(self):
            return super().closable - self.restricted_stock

        @property
        def trading_pnl(self):
            traded = self.quantity - self._logical_old_quantity - self._credited_today
            return (traded * self.last_price - self._trade_cost) * self._direction_factor

        @property
        def position_pnl(self):
            held = self._logical_old_quantity + self._pending_begin
            movement = held * (self.last_price - self.unadjusted_prev_close) if held else 0
            return (
                movement + self._new_today * self.last_price + self._daily_dividend
            ) * self._direction_factor

        def before_trading(self, trading_date):
            self._pending_begin = self.pending_stock
            self._new_today = self._credited_today = 0
            delta = Position.before_trading(self, trading_date)
            self._unadjusted_prev_close = self.last_price
            self._daily_dividend, self._daily_split = 0, 1
            if self.direction != POSITION_DIRECTION.LONG:
                require(not self.entitlements, "CORPORATE_SHORT_POSITION_UNSUPPORTED")
                return delta
            day = str(trading_date)
            with localcontext(prec=50):
                # Every ex-date basis was fixed at the preceding close. Resolve
                # those bases before any earlier plan credits shares today.
                # Pending shares keep their own allocated cost until credit;
                # they do not become part of a later plan's registered holding.
                for entry in self.entitlements.values():
                    event = entry["event"]
                    if event["ex_date"] <= day and not self.done(entry, "ex"):
                        require(self.quantity == entry["basis"], "CORPORATE_RECORD_QUANTITY_DRIFT")
                        total = self.quantity + entry["shares"]
                        unit = (
                            (Decimal(str(self._avg_price)) * self.quantity - Decimal(entry["cash"]))
                            / total
                            if total
                            else Decimal(0)
                        )
                        self._avg_price = float(unit)
                        entry["pending_cost"] = str(unit * entry["shares"])
                        self._daily_dividend += float(entry["cash"])
                        self._new_today += entry["shares"]
                        # All economic shares share the provisional ex mark,
                        # including earlier entitlements not yet credited. The
                        # transfer must preserve equity before an observed bar.
                        economic_quantity = self.quantity + self.pending_stock
                        economic_total = economic_quantity + entry["shares"]
                        if economic_total:
                            self._last_price = float(
                                (
                                    Decimal(str(self.last_price)) * economic_quantity
                                    - Decimal(entry["cash"])
                                )
                                / economic_total
                            )
                        self.mark(entry, "ex")
                for entry in self.entitlements.values():
                    event = entry["event"]
                    if (
                        self.done(entry, "ex")
                        and Decimal(event["cash_per_share"])
                        and event["payment_date"] <= day
                        and not self.done(entry, "cash")
                    ):
                        delta += float(entry["cash"])
                        self.mark(entry, "cash")
                    stock = Decimal(event["bonus_share_ratio"]) + Decimal(
                        event["capitalization_share_ratio"]
                    )
                    if (
                        stock
                        and event["share_credit_date"] <= day
                        and not self.done(entry, "stock_credit")
                    ):
                        added = entry["shares"]
                        total = self.quantity + added
                        if total:
                            self._avg_price = float(
                                (
                                    Decimal(str(self._avg_price)) * self.quantity
                                    + Decimal(entry["pending_cost"])
                                )
                                / total
                            )
                        self._quantity += added
                        self._credited_today += added
                        self._old_quantity += added
                        self._queue.handle_trade(
                            added, date.fromisoformat(event["share_credit_date"])
                        )
                        self.mark(entry, "stock_credit")
                    if (
                        stock
                        and event["new_shares_tradable_date"] <= day
                        and not self.done(entry, "stock_tradable")
                    ):
                        require(self.done(entry, "stock_credit"), "CORPORATE_UNLOCK_BEFORE_CREDIT")
                        self.mark(entry, "stock_tradable")
            return delta

        def get_state(self):
            return {
                **super().get_state(),
                "corporate_entitlements_v1": deepcopy(self.entitlements),
                "corporate_pnl_v1": {
                    name: getattr(self, name)
                    for name in ("_pending_begin", "_new_today", "_credited_today")
                },
            }

        def set_state(self, state):
            super().set_state(state)
            self.entitlements = deepcopy(state["corporate_entitlements_v1"])
            for name in ("_pending_begin", "_new_today", "_credited_today"):
                setattr(self, name, state["corporate_pnl_v1"][name])
            current = {e["entitlement_key"]: digest(e) for e in host.data.corporate_actions}
            for key, entry in self.entitlements.items():
                require(
                    current.get(key) == entry["source_sha256"],
                    "CORPORATE_REVISION_RECONCILIATION_REQUIRED",
                )

    require(
        POSITION_TYPE_MAP[INSTRUMENT_TYPE.CS] is StockPosition, "CORPORATE_NATIVE_POSITION_REGISTRY"
    )
    POSITION_TYPE_MAP[INSTRUMENT_TYPE.CS] = CorporateStockPosition
