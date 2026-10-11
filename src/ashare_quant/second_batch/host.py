"""Execute the hash-bound original script through native RQAlpha seams."""

import json
from decimal import Decimal
from pathlib import Path

from rqalpha.const import EXECUTION_PHASE, INSTRUMENT_TYPE
from rqalpha.core.events import EVENT, Event
from rqalpha.core.execution_context import ExecutionContext

from ashare_quant.execution_six.host import Host as BaseHost
from ashare_quant.execution_six.host import Mod as BaseMod
from ashare_quant.round_one.dividend_tax import MODEL, install_fixed_tax_position
from ashare_quant.round_one.premarket import admit, apply_reference_mark

from .facade import Context, install
from .native import Board, Matcher, Source


class Host(BaseHost):
    def __init__(self, case, owner, metadata, plan, root):
        super().__init__(case, owner, metadata, plan, root)
        self.owner = owner
        self.previous = metadata["previous"]
        self.action = owner.call("actions") if case.case_id == "B45" else None
        self.context = Context(self)
        self.module = install(self)
        self.scope = {}
        self.gaps = []
        self.order_costs = {}
        exec(compile(Path(case.script).read_text(), case.script, "exec"), self.scope)
        if self.scope["CONFIG"] != case.config:
            raise RuntimeError("SECOND_SCRIPT_CONFIG_MISMATCH")

    def security(self, sid):
        if self.closed or sid not in self.case.securities:
            raise RuntimeError("SECOND_SECURITY_SCOPE")

    def check_option(self, k, v):
        if self.case.config.get(k) != v:
            raise RuntimeError("SECOND_CONFIG_MISMATCH:" + k)

    def check_cost(self, cost, kind):
        if kind != "stock" or any(self.case.config.get(k) != v for k, v in cost.items()):
            raise RuntimeError("SECOND_COST_CONFIG_MISMATCH")

    def initialize(self):
        self.scope["initialize"](self.context)
        self.started = True

    def quote(self, sid):
        self.security(sid)
        value = self.owner.call("engine_quote", security=sid, as_of=self.at.isoformat())
        if value["status"] != "available":
            raise RuntimeError("SECOND_QUOTE_MISSING:" + sid + ":" + self.at.isoformat())
        values = value["rows"][0]["values"]
        return {k: v if type(v) is bool else float(v) for k, v in values.items()}

    def daily(self, sid, day, field):
        self.security(sid)
        if sid == self.case.security:
            product = "b45-daily" if self.case.day.year == 2016 else "b40-44-daily"
            answer = self.owner.call("daily", product=product, date=day)
            if len(answer["rows"]) != 1:
                raise RuntimeError("SECOND_DAILY_MISSING:" + sid + ":" + day)
            self.emit(
                "independent_daily_research_read",
                security=sid,
                date=day,
                field=field,
                purpose="retrospective_capacity" if field == "volume" else "valuation",
                product_id=product,
                matching_price=False,
                strategy_visible=False,
            )
            return float(answer["rows"][0]["fields"][field]["value"])
        role = (
            "engine_capacity"
            if field == "volume"
            else "final_valuation"
            if day == str(self.at.date())
            else "initialization"
        )
        answer = self.owner.call(
            "legacy",
            security=sid,
            kind="daily",
            role=role,
            as_of=self.at.isoformat(),
            start=day + "T00:00:00+08:00",
            end=day + "T00:00:00+08:00",
        )
        if len(answer["rows"]) != 1:
            raise RuntimeError("SECOND_LEGACY_DAILY_MISSING:" + sid + ":" + day)
        return float(answer["rows"][0]["values"][field])

    def dispatch(self):
        callback = self.callbacks.get(self.at.strftime("%H:%M"))
        if callback is not None:
            with ExecutionContext(EXECUTION_PHASE.SCHEDULED):
                callback(self.context)

    def user_event(self, event):
        if event.aq_kind == "end_clock":
            return
        account = self.env.portfolio.stock_account
        # Refresh marks only where AD supplies a point. Retain native last mark
        # with an explicit gap otherwise; never label that stale value current.
        for pos in account.get_positions():
            if not pos.quantity:
                continue
            try:
                value = self.quote(pos.order_book_id)["last_price"]
            except RuntimeError as exc:
                if not str(exc).startswith("SECOND_QUOTE_MISSING:"):
                    raise
                if self.case.policy == "integrated":
                    point = self.owner.call(
                        "valuation_point", security=pos.order_book_id, at=self.at.isoformat()
                    )
                    if point.get("rows"):
                        value = float(point["rows"][0]["values"]["price"])
                        apply_reference_mark(pos, value)
                        self.emit(
                            "independent_point_valuation",
                            public=point,
                            matching_eligible=False,
                            strategy_visible=False,
                        )
                        continue
                self.gaps.append(
                    dict(at=self.at.isoformat(), security=pos.order_book_id, reason=str(exc))
                )
                self.emit("valuation_gap", **self.gaps[-1])
            else:
                pos.update_last_price(value)
        self.dispatch()
        # Resting orders are matched after the callback at the source minute.
        # New orders already used native immediate matching and must not double-fill.
        active = self.env.broker.get_open_orders()
        if active and all(o.datetime < self.at.replace(tzinfo=None) for o in active):
            self.env.broker.on_bar(Event(EVENT.BAR))

    def before(self):
        if (
            self.case.policy == "integrated"
            and self.action
            and str(self.at.date()) == self.action["rows"][0]["ex_date"]
        ):
            if self.env.broker.get_open_orders():
                raise RuntimeError("SECOND_PREMARKET_PENDING_ORDERS")
            row = self.action["rows"][0]
            facts = row["facts"]
            fact = dict(
                ex_date=row["ex_date"],
                record_date=facts["record_date"],
                gross_cash_per_share=str(Decimal(facts["gross_cash_per_10"]) / 10),
                capitalization_share_ratio=str(Decimal(facts["capitalization_shares_per_10"]) / 10),
            )
            public = self.owner.call(
                "premarket_reference", security=self.case.security, as_of=self.at.isoformat()
            )
            price = admit(
                public,
                security=self.case.security,
                as_of=self.at.isoformat(),
                fact=fact,
                raw_previous_close=str(
                    self.daily(self.case.security, facts["record_date"], "close")
                ),
            )
            before = self.account()
            for position in self.env.portfolio.stock_account.get_positions():
                if position.quantity and position.order_book_id == self.case.security:
                    apply_reference_mark(position, price)
            after = self.account()
            if any(before[k] != after[k] for k in ["cash", "available_cash", "frozen_cash"]):
                raise RuntimeError("SECOND_PREMARKET_CASH_CHANGED")
            self.emit(
                "premarket_reference_transition",
                public=public,
                before=before,
                after=after,
                phase="after native dividend and capitalization, before original day_start",
            )
        self.scope["before_trading_start"](self.context)

    def prepare_open(self, event):
        if any(p.quantity for p in self.env.portfolio.stock_account.get_positions()):
            self.env.portfolio.stock_account._on_bar(event)

    def after(self):
        self.scope["after_trading_end"](self.context)

    def complete(self):
        self.scope["on_strategy_end"](self.context)
        # Always seal the recorder even if the source's optional export handler failed.
        original = [json.loads(s) for s in self.module.g.events]
        (self.root / "original-events.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in original) + "\n"
        )
        return dict(
            case_id=self.case.case_id,
            submissions=self.submissions,
            orders=[self.project_order(o) for o in self.orders.values()],
            trades=self.trades,
            final_account=self.account(),
            gaps=self.gaps,
            source_not_triggered=[e for e in original if e["kind"] == "not_triggered"],
            source_errors=[
                e for e in original if e["kind"] in {"order_exception", "observation_error"}
            ],
            native_classes={
                k: type(getattr(self.env, k)).__module__ + "." + type(getattr(self.env, k)).__name__
                for k in ["portfolio", "broker", "data_source", "event_source"]
            },
            account_class=type(self.env.portfolio.stock_account).__module__
            + "."
            + type(self.env.portfolio.stock_account).__name__,
            original_script_executed=True,
            actual_real_case_run=True,
            full_market_simulation=False,
            historical_pit=False,
            comparison="NOT_YET_PERFORMED",
        )


class Mod(BaseMod):
    source_type = Source

    def start_up(self, env, config):
        super().start_up(env, config)
        env.set_price_board(Board(self.host))
        if self.host.case.policy == "integrated":
            from .reservation import install_budget_account

            install_budget_account(env)
        if self.host.case.case_id == "B45":
            install_fixed_tax_position(env, dict(schema=MODEL, rate="0.20"))
        env.event_bus.add_listener(EVENT.PRE_BEFORE_TRADING, self.host.prepare_open)

    def ready(self, event):
        h = self.host
        h.env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, Matcher(h, h.env.config.mod.sys_simulation)
        )


def load_mod():
    return Mod()
