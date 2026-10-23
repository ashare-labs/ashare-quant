import json
from pathlib import Path

from rqalpha.const import POSITION_DIRECTION
from rqalpha.core.events import EVENT
from rqalpha.interface import AbstractMod

from ashare_quant.execution.rqalpha.adapter import DataSource, PriceBoard
from ashare_quant.execution.rqalpha.checkpoint import AtomicFile
from ashare_quant.execution.rqalpha.events import RecordedEventSource
from ashare_quant.execution.rqalpha.guards import block_rqdata_apis
from tests.support.rqalpha.fixture import SID, SyntheticProvider

__config__ = {"priority": 1000}


class ProbeMod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.config = env, config
        provider = SyntheticProvider(
            actions=config.actions, missing=config.missing, tplus=config.tplus
        )
        self.provider = provider
        ds = DataSource(env, provider)
        env.set_data_source(ds)
        env.set_price_board(PriceBoard(ds))
        if config.recorded:
            records = json.loads(Path(config.events_path).read_text())
            env.set_event_source(RecordedEventSource(env, records))
        if env.config.base.persist:
            env.set_persist_provider(AtomicFile(config.checkpoint, config.identity))
        self.blocked = block_rqdata_apis()
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        for event in [
            EVENT.PRE_BAR,
            EVENT.POST_BAR,
            EVENT.PRE_BEFORE_TRADING,
            EVENT.POST_BEFORE_TRADING,
            EVENT.PRE_SETTLEMENT,
            EVENT.POST_SETTLEMENT,
        ]:
            env.event_bus.add_listener(event, self.observe)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)

    def trade(self, event):
        t = event.trade
        self.env.user_strategy.user_context.trades.append(
            dict(
                dt=str(t.trading_datetime),
                id=t.exec_id,
                order_id=t.order_id,
                price=t.last_price,
                quantity=t.last_quantity,
                side=str(t.side),
                commission=t.commission,
                tax=t.tax,
            )
        )

    def observe(self, event):
        c = self.env.user_strategy.user_context
        c.trace.append(
            dict(event=event.event_type.name, dt=str(self.env.calendar_dt), **self.state())
        )
        # Kills inside Executor's multi-phase event, before the source cursor commit.
        from ashare_quant.execution.rqalpha.checkpoint import kill_at

        kill_at(event.event_type.name.lower(), getattr(self.env.event_source, "cursor", 0) + 1)

    def state(self):
        p = self.env.portfolio.get_position(SID, POSITION_DIRECTION.LONG)
        return dict(
            cash=self.env.portfolio.cash,
            quantity=p.quantity,
            closable=p.closable,
            average=p.avg_price,
            receivable=p.dividend_receivable,
            equity=self.env.portfolio.total_value,
            transaction_cost=p.transaction_cost,
        )

    def finish(self, event):
        self.result = self.receipt()
        if self.env.config.base.persist:
            self.env.persist_helper.receipt = self.result
            self.env.persist_helper.complete = True
            self.env.persist_helper.persist()

    def receipt(self):
        c = self.env.user_strategy.user_context
        result = dict(
            identity=self.config.identity,
            evidence_kind="synthetic",
            state=self.state(),
            trace=c.trace,
            trades=c.trades,
            probes=c.probes,
            queries=self.provider.queries,
            blocked_apis=self.blocked,
            matching_type=str(self.env.config.mod.sys_simulation.matching_type),
            classes={
                name: type(getattr(self.env, name)).__module__
                + "."
                + type(getattr(self.env, name)).__name__
                for name in ["broker", "portfolio", "event_source", "data_source", "price_board"]
            },
        )
        result["classes"]["account"] = (
            type(self.env.portfolio.stock_account).__module__
            + "."
            + type(self.env.portfolio.stock_account).__name__
        )
        position = self.env.portfolio.get_position(SID, POSITION_DIRECTION.LONG)
        result["classes"]["position"] = type(position).__module__ + "." + type(position).__name__
        return result

    def tear_down(self, code, exception=None):
        if exception is not None:
            return
        Path(self.config.result).write_text(json.dumps(self.result, indent=2, default=str))
        return self.result


def load_mod():
    return ProbeMod()
