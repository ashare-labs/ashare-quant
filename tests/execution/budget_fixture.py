"""Synthetic native account counterexamples, independent of B44 prices and quantities."""

# ruff: noqa: E402 -- install offline guard before engine modules.
import argparse
import copy
import json
from pathlib import Path

from ashare_quant.execution.rqalpha.guards import install_network_guard

install_network_guard()
from rqalpha.core.events import EVENT, Event

from ashare_quant.execution.reservation import install_budget_account
from tests.support.rqalpha.fixture import SyntheticProvider
from tests.support.rqalpha.mod import ProbeMod


class BudgetProbe(ProbeMod):
    def start_up(self, env, config):
        super().start_up(env, config)
        install_budget_account(env)
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.guards)
        env.event_bus.add_listener(EVENT.PRE_BAR, self.cancel)

    def guards(self, event):
        self.env.event_bus.add_listener(EVENT.TRADE, self.replay)
        account = self.env.portfolio.stock_account
        for call in (
            lambda: account.set_state(account.get_state()),
            lambda: account.fast_forward(orders=[]),
        ):
            try:
                call()
            except RuntimeError as exc:
                assert str(exc) == "BUDGET_CHECKPOINT_RESTORE_UNSUPPORTED"
            else:
                raise AssertionError("Unexpected restore admission")

    def replay(self, event):
        account = event.account
        state = copy.deepcopy(account.get_state())
        account.apply_trade(event.trade, event.order)
        assert account.get_state() == state
        changed = copy.copy(event.trade)
        changed._price += 0.01
        try:
            account.apply_trade(changed, event.order)
        except RuntimeError as exc:
            assert str(exc) == "BUDGET_TRADE_ID_COLLISION"
        else:
            raise AssertionError("Unexpected trade identity collision admission")
        assert account.get_state() == state

    def cancel(self, event):
        if self.env.calendar_dt.strftime("%H:%M") != "09:33":
            return
        c = self.env.user_strategy.user_context
        account = self.env.portfolio.stock_account
        for order in list(self.env.broker.get_open_orders()):
            self.env.broker.cancel_order(order)
            state = copy.deepcopy(account.get_state())
            account._on_order_unsolicited_update(
                Event(EVENT.ORDER_CANCELLATION_PASS, account=account, order=order)
            )
            assert account.get_state() == state
        c.probes.append(
            dict(
                kind="cancel",
                cash=account.cash,
                frozen=account.frozen_cash,
                total=account.total_cash,
            )
        )


def main():
    import rqalpha.main
    import rqalpha.utils.config as configs

    import tests.support.rqalpha.mod as mod

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    events = out.with_suffix(".events.json")
    events.write_text(
        json.dumps(SyntheticProvider().records("1m", start="2024-01-04", end="2024-01-04"))
    )
    mod.ProbeMod = BudgetProbe
    source = (
        f"MODE={args.scenario!r}\n" + Path(__file__).with_name("budget_strategy.py").read_text()
    )
    config = dict(
        base=dict(
            start_date="2024-01-04",
            end_date="2024-01-04",
            frequency="1m",
            accounts={"stock": 1000 if args.scenario == "fee_rejection" else 20000},
            run_type="b",
            rqdatac_uri="disabled",
            auto_update_bundle=False,
            data_bundle_path=str(out.parent / "ABSENT"),
            capital_gain_tax_rate=0,
            partial_fill_on_insufficient_cash=False,
        ),
        extra=dict(log_level="error", locale="en_US"),
        mod=dict(
            sys_progress={"enabled": False},
            sys_analyser={"enabled": False},
            sys_simulation=dict(matching_type="current_bar", volume_percent=0.25, slippage=0),
            sys_transaction_cost=dict(
                stock_min_commission=5,
                stock_commission_multiplier=1,
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
                enabled=True,
                lib="tests.support.rqalpha.mod",
                priority=1000,
                actions=False,
                missing=[],
                tplus=1,
                recorded=True,
                result=str(out),
                events_path=str(events),
                identity="synthetic-budget",
            ),
        ),
    )
    configs.user_config = configs.project_config = lambda: {}

    def forbidden(*args, **kwargs):
        raise RuntimeError("DEFAULT_BUNDLE_DISABLED")

    rqalpha.main.BaseDataSource = forbidden
    rqalpha.main.run(configs.parse_config(config, source_code=source), source_code=source)
