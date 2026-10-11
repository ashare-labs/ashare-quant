"""Synthetic one-session native replay fixture; nothing is claimed as real data."""

# ruff: noqa: E402
import argparse
import copy
import json
from datetime import timedelta
from pathlib import Path

from rqprobe.guards import install_network_guard

install_network_guard()

from rqalpha.core.events import EVENT, Event
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade

from ashare_quant.round_one.reservation_account import MODEL, install_reservation_account
from rqprobe.checkpoint import AtomicFile, Counter, StrictHelper
from rqprobe.fixture import SyntheticProvider
from rqprobe.mod import ProbeMod
from rqprobe.runtime import checkpoint_identity


class ReservationProbe(ProbeMod):
    def start_up(self, env, config):
        super().start_up(env, config)
        install_reservation_account(env, MODEL)
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.restore_guards)
        env.event_bus.add_listener(EVENT.PRE_BAR, self.cancel_before_match)

    def restore_guards(self, event):
        self.env.event_bus.add_listener(EVENT.TRADE, self.verify_trade)
        account = self.env.portfolio.stock_account
        state = copy.deepcopy(account.get_state())
        legacy = copy.deepcopy(state)
        del legacy["aq_reservation"]
        try:
            account.set_state(legacy)
        except RuntimeError as exc:
            assert str(exc) == "RESERVATION_CHECKPOINT_MODEL_REQUIRED"
        else:
            raise AssertionError("UNBOUND_RESTORE_ACCEPTED")
        assert account.get_state() == state

    def verify_trade(self, event):
        account = event.account
        state = copy.deepcopy(account.get_state())
        account.apply_trade(event.trade, event.order)
        assert account.get_state() == state
        changed = copy.copy(event.trade)
        changed._price += 0.01
        try:
            account.apply_trade(changed, event.order)
        except RuntimeError as exc:
            assert "ID_COLLISION" in str(exc)
        else:
            raise AssertionError("TRADE_ID_COLLISION_ACCEPTED")
        assert account.get_state() == state
        for key, value in [
            ("frozen_cash", state["frozen_cash"] + 0.01),
            ("backward_trade_set", set()),
        ]:
            corrupt = copy.deepcopy(state)
            corrupt[key] = value
            try:
                account.set_state(corrupt)
            except RuntimeError as exc:
                assert str(exc) == "RESERVATION_CHECKPOINT_FREEZE_OR_REPLAY_MISMATCH"
            else:
                raise AssertionError("CORRUPT_RESERVATION_RESTORE_ACCEPTED")
            assert account.get_state() == state
        clock = self.env.calendar_dt, self.env.trading_dt
        self.env.update_time(*(x + timedelta(days=1) for x in clock))
        try:
            account._on_before_trading(Event(EVENT.BEFORE_TRADING))
        except RuntimeError as exc:
            assert str(exc) == "RESERVATION_CROSS_SESSION_UNSUPPORTED"
        else:
            raise AssertionError("CROSS_SESSION_ACCEPTED")
        finally:
            self.env.update_time(*clock)
        assert account.get_state() == state
        # The account can rebuild its one active reservation using native order facts.
        account.fast_forward(orders=[event.order], trades=[event.trade])
        assert account.get_state() == state

    def cancel_before_match(self, event):
        context = self.env.user_strategy.user_context
        if self.config.scenario == "second_fill" or context.now.strftime("%H:%M") != "09:32":
            return
        account = self.env.portfolio.stock_account
        assert len(context.trades) == 1
        # Restored strategy state stores identity, not a stale broker object.
        pending = next(
            o for o in self.env.broker.get_open_orders() if o.order_id == context.pending
        )
        self.env.broker.cancel_order(pending)

        def snap(label):
            context.probes.append(
                dict(
                    kind=label,
                    at=context.now.isoformat(),
                    cash=account.cash,
                    total_cash=account.total_cash,
                    frozen_cash=account.frozen_cash,
                    account_state=copy.deepcopy(account.get_state()),
                )
            )

        snap("cancelled")
        state = copy.deepcopy(account.get_state())
        account._on_order_unsolicited_update(
            Event(EVENT.ORDER_CANCELLATION_PASS, account=account, order=pending)
        )
        assert account.get_state() == state
        account.fast_forward(orders=[pending])
        assert account.get_state() == state
        snap("duplicate_cancel")

    def state(self):
        value = super().state()
        account = self.env.portfolio.stock_account
        return dict(
            value,
            frozen_cash=account.frozen_cash,
            total_cash=account.total_cash,
            reservation=account.get_state()["aq_reservation"],
        )


def main():
    import rqalpha.main
    import rqalpha.utils.config as configs

    import rqprobe.mod as mod

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--scenario", default="partial")
    args = parser.parse_args()
    out = Path(args.out).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    mod.ProbeMod = ReservationProbe
    records = SyntheticProvider().records("1m", start="2024-01-04", end="2024-01-04")
    events = out.with_suffix(".events.json")
    events.write_text(json.dumps(records))
    source = (
        f"MODE={args.scenario!r}\n"
        + Path(__file__).with_name("reservation_strategy.py").read_text()
    )
    requested = dict(
        base=dict(
            start_date="2024-01-04",
            end_date="2024-01-04",
            frequency="1m",
            accounts={"stock": 10000},
            run_type="b",
            rqdatac_uri="disabled",
            auto_update_bundle=False,
            data_bundle_path=str(out.parent / "ABSENT"),
            persist=bool(args.checkpoint),
            persist_mode="real_time",
            capital_gain_tax_rate=0,
            partial_fill_on_insufficient_cash=False,
        ),
        extra=dict(log_level="error", locale="en_US"),
        mod=dict(
            sys_progress={"enabled": False},
            sys_analyser={"enabled": False},
            sys_simulation=dict(matching_type="current_bar", volume_percent=0.25, slippage=0),
            sys_transaction_cost=dict(
                stock_min_commission=0,
                stock_commission_multiplier=1 if args.scenario == "fee" else 0,
                tax_multiplier=0,
                pit_tax=False,
            ),
            sys_accounts=dict(
                dividend_tax_enabled=False,
                dividend_reinvestment=False,
                stock_t1=True,
                financing_rate=0,
            ),
            probe=dict(
                scenario=args.scenario,
                enabled=True,
                lib="rqprobe.mod",
                priority=1000,
                actions=False,
                missing=[],
                tplus=1,
                recorded=True,
                result=str(out),
                events_path=str(events),
                checkpoint=args.checkpoint,
                identity="",
            ),
        ),
    )
    configs.user_config = configs.project_config = lambda: {}
    config = configs.parse_config(requested, source_code=source)
    config.mod.probe.identity = checkpoint_identity(config, source, records, __file__)
    if args.checkpoint:
        payload = AtomicFile(args.checkpoint, config.mod.probe.identity).read()
        if payload and payload["complete"]:
            out.write_text(json.dumps(payload["receipt"], indent=2, default=str))
            return
        rqalpha.main.PersistHelper = StrictHelper
    Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)

    def forbidden(*args, **kwargs):
        raise RuntimeError("DEFAULT_BUNDLE_DISABLED")

    rqalpha.main.BaseDataSource = forbidden
    rqalpha.main.run(config, source_code=source)
