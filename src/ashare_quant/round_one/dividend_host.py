"""Known-cash-event adapter. Native positions/account own entitlement and posting."""

import sys
from datetime import date
from decimal import Decimal

from rqalpha.const import POSITION_DIRECTION
from rqalpha.core.events import EVENT
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition

from ashare_quant.execution_six.contracts import owner_price

from .carry_contract import admit_dividend_execution
from .carry_host import API, CarryInstrument, Proxy
from .carry_host import Board as CarryBoard
from .carry_host import Host as CarryHost
from .carry_host import Source as CarrySource
from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod
from .premarket import apply_reference_mark
from .queries import QueryRefused


class Instrument(CarryInstrument):
    def de_listed_at(self, dt):
        h = self.host
        if (
            sys._getframe(1).f_code is StockPosition.settlement.__code__
            and str(h.at.date()) == h.case.end_day
            and str(dt.date() if hasattr(dt, "date") else dt)
            == h.metadata["calendar_links"][h.case.end_day]["next_session"]
        ):
            h.emit(
                "terminal_identity_model",
                basis="explicit_research_assumption",
                future_status=None,
                position_retained=True,
                forced_liquidation=False,
            )
            return False
        return super().de_listed_at(dt)


class Source(CarrySource):
    def __init__(self, host):
        self.host, self.instrument = host, Instrument(host)

    def get_dividend(self, instrument):
        self.host.security(instrument.order_book_id)
        fact = self.host.read("cash_fact")
        values = admit_dividend_execution(
            fact,
            security=instrument.order_book_id,
            as_of=self.host.at,
            next_session=lambda d: date.fromisoformat(
                self.host.metadata["calendar_links"][str(d)]["next_session"]
            ),
            model=self.host.provider.model["account_model"],
        )
        self.host.emit(
            "native_dividend_records",
            source="AD official cash fact",
            rows=[dict(zip(values.dtype.names, r.tolist())) for r in values],
            account_calculation="native StockPosition",
            strategy_visible=False,
        )
        return values


class Board(CarryBoard):
    def get_last_price(self, sid):
        h = self.host
        h.security(sid)
        if h.at.isoformat() == "2024-06-14T10:00:00+08:00":
            return owner_price(h.read("point")["price"])
        return super().get_last_price(sid)


class Host(CarryHost):
    proxy_class = Proxy

    def __init__(self, *args):
        # Reuse projections, not the six-case sell/liquidation intent.
        FeeHost.__init__(self, *args)
        self.api = API(self)
        self.intents = [("2024-06-13", self.case.quantity, None)]
        self.record_close = None
        self.before_credit = None

    def prepare_open(self, event):
        if str(self.at.date()) == self.case.end_day:
            if self.record_close is None or self.env.broker.get_open_orders():
                raise RuntimeError("B26_RECORD_CLOSE_POSITION_REQUIRED")
            current = self.account()
            if current["positions"] != self.record_close["positions"]:
                raise RuntimeError("B26_POSITION_CHANGED_AFTER_RECORD_CLOSE")
            # Refresh raw prior close BEFORE native ex-date adjustment; never
            # overwrite the adjusted native mark afterwards with raw close.
            self.env.portfolio.stock_account._on_bar(event)
            self.before_credit = self.account()
            self.emit(
                "native_dividend_before",
                account=self.before_credit,
                eligibility="actual_virtual_record_close_carried_unchanged",
            )

    def before(self):
        self.emit(
            "day_start",
            previous_date=self.metadata["calendar_links"][str(self.at.date())]["previous_session"],
        )
        if str(self.at.date()) == self.case.end_day:
            if self.before_credit is None:
                raise RuntimeError("B26_NATIVE_DIVIDEND_PHASE_ORDER_INVALID")
            after = self.account()
            before = self.before_credit
            quantity = before["positions"][0]["quantity"]
            gross = Decimal(self.provider.fact["gross_cash_per_share"]) * quantity
            net = gross * (
                1 - Decimal(self.provider.model.get("tax_compatibility", {}).get("rate", "0"))
            )
            delta = Decimal(after["cash"]) - Decimal(before["cash"])
            cost_delta = Decimal(before["positions"][0]["average_price"]) - Decimal(
                after["positions"][0]["average_price"]
            )
            self.emit(
                "native_dividend_transition",
                before=before,
                after=after,
                source_gross_per_share=self.provider.fact["gross_cash_per_share"],
                actual_cash_delta=str(delta),
                expected_gross_for_actual_quantity=str(gross),
                expected_credit_for_actual_quantity=str(net),
                observed_cost_reduction=str(cost_delta),
                payment_phase="PRE_BEFORE_TRADING",
                official_intraday_time=False,
                tax_model=(
                    "jq_fixed_20_percent_simulation"
                    if "tax_compatibility" in self.provider.model
                    else "gross_no_withholding_or_deferred_tax"
                ),
                position_owner=type(
                    self.env.portfolio.get_position(self.case.security, POSITION_DIRECTION.LONG)
                ).__name__,
            )
            if "premarket_reference" in self.provider.model:
                if self.env.broker.get_open_orders():
                    raise RuntimeError("B26_PREMARKET_REQUIRES_NO_PENDING_ORDERS")
                value = self.read(
                    "premarket_reference", raw_previous_close=before["positions"][0]["last_price"]
                )
                position = self.env.portfolio.get_position(
                    self.case.security, POSITION_DIRECTION.LONG
                )
                apply_reference_mark(position, owner_price(value["price"]))
                marked = self.account()
                if any(
                    marked[k] != after[k] for k in ("cash", "transferable_cash", "cash_settlement")
                ):
                    raise RuntimeError("B26_PREMARKET_CASH_CHANGED")
                self.emit(
                    "premarket_reference_transition",
                    before=after,
                    after=marked,
                    native_position_state_unchanged=True,
                    historical_pit=False,
                    strategy_visible=False,
                    matching_eligible=False,
                    phase="after native dividend before day_start snapshot",
                    source="AD source-native prevclose reconstruction",
                )
        self.snapshot("day_start")

    def user_event(self, event):
        if event.aq_kind == "point":
            if self.env.broker.get_open_orders():
                raise RuntimeError("B26_POINT_VALUATION_REQUIRES_NO_PENDING_ORDERS")
            self.emit(
                "point_valuation_model",
                matching=False,
                strategy_visible=False,
                source="AD retrospective TDX point; no OHLC synthesis",
            )
            self.env.portfolio.stock_account._on_bar(event)
        super().user_event(event)

    def after(self):
        self.snapshot("end_of_day")
        if str(self.at.date()) == str(self.case.day):
            self.record_close = self.account()
            self.emit(
                "record_close_position",
                account=self.record_close,
                entitlement_not_assumed_until_native_next_open=True,
            )
        op = self.provider.query_operation(self.at)
        self.emit("query_request", **op)
        seq = self.records[-1]["seq"]
        try:
            value = self.provider.queries.query(
                **{k: op[k] for k in ("api", "parameters", "as_of", "role")}
            )
            self.emit("query_return", request_seq=seq, label=op["label"], value=value)
        except QueryRefused as exc:
            self.emit("query_exception", request_seq=seq, label=op["label"], error=exc.to_dict())

    def complete(self):
        result = FeeHost.complete(self)
        credits = [r for r in self.records if r["kind"] == "native_dividend_transition"]
        final = result["final_account"]
        record = self.record_close
        successful = (
            self.submissions == 1
            and len(self.orders) == 1
            and len(self.trades) == 1
            and self.trades[0]["quantity"] == self.case.quantity
            and record is not None
            and len(record["positions"]) == 1
            and record["positions"][0]["quantity"] == self.case.quantity
            and len(final["positions"]) == 1
            and final["positions"][0]["quantity"] == self.case.quantity
            and not self.env.broker.get_open_orders()
            and len(credits) == 1
        )
        if successful:
            e = credits[0]
            successful = abs(
                Decimal(e["actual_cash_delta"]) - Decimal(e["expected_credit_for_actual_quantity"])
            ) < Decimal("1e-9") and abs(
                Decimal(e["observed_cost_reduction"]) - Decimal(e["source_gross_per_share"])
            ) < Decimal("1e-9")
        result.update(
            scenario_triggered=bool(successful),
            status="SCENARIO_EXECUTED" if successful else "SCENARIO_NOT_TRIGGERED",
            research_classification="CONDITIONAL_KNOWN_CASH_DIVIDEND_EXECUTION",
            model=self.provider.model,
            known_facts=self.provider.facts,
            cash_fact=self.provider.fact,
            unknowns=self.provider.facts["required_unknowns"]
            + ["specific_account_registration", "account_tax", "official_intraday_credit_time"],
            assumptions=self.provider.model["hypotheses"],
            data_gaps=self.provider.binding["gaps"],
            historical_pit=False,
            general_engine_equivalence=False,
            native_dividend_transitions=credits,
            premarket_reference_transitions=[
                r for r in self.records if r["kind"] == "premarket_reference_transition"
            ],
            original_requests_submitted=self.submissions,
            holding_at_end=True,
        )
        return result


class Mod(FeeMod):
    source_type = Source

    def start_up(self, env, config):
        super().start_up(env, config)
        env.set_price_board(Board(self.host))
        if "tax_compatibility" in self.host.provider.model:
            from .dividend_tax import install_fixed_tax_position

            install_fixed_tax_position(env, self.host.provider.model["tax_compatibility"])
        # Registered before INIT_PORTFOLIO constructs the native account listeners.
        env.event_bus.add_listener(EVENT.PRE_BEFORE_TRADING, self.host.prepare_open)


def load_mod():
    return Mod()
