"""Synthetic three-session oracle; Friday -> Monday is explicitly supplied data."""
# ruff: noqa: E402 -- deny network before importing the native runtime.

import argparse
import copy
import json
from pathlib import Path

import pandas as pd

from rqprobe.guards import install_network_guard

install_network_guard()

from rqalpha.core.events import EVENT
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade

from ashare_quant.round_one.cash_account import install_cash_account
from rqprobe.checkpoint import AtomicFile, Counter, StrictHelper
from rqprobe.fixture import SID, SyntheticProvider
from rqprobe.mod import ProbeMod
from rqprobe.runtime import checkpoint_identity


class Provider(SyntheticProvider):
    calendar = pd.DatetimeIndex(
        ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
    )


class CashProbe(ProbeMod):
    def start_up(self, env, config):
        super().start_up(env, config)
        if getattr(config, "fixed_dividend_tax", False):
            from ashare_quant.round_one.dividend_tax import install_fixed_tax_position

            install_fixed_tax_position(env, dict(schema="aq.jq-fixed-dividend-tax.v1", rate="0.20"))
        install_cash_account(env)
        env.event_bus.add_listener(EVENT.POST_SETTLEMENT, self.repeat_settlement)
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.restore_guards)

    def restore_guards(self, event):
        account = self.env.portfolio.stock_account
        before = copy.deepcopy(account.get_state())
        legacy = copy.deepcopy(before)
        del legacy["aq_cash"]
        try:
            account.set_state(legacy)
        except RuntimeError as exc:
            assert str(exc) == "CASH_CHECKPOINT_MODEL_REQUIRED"
        else:
            raise AssertionError("LEGACY_CASH_RESTORE_ACCEPTED")
        assert account.get_state() == before
        account.deposit_withdraw(25)
        assert account.transferable_cash == before["total_cash"] + 25
        account.deposit_withdraw(-25)
        assert account.get_state() == before
        for operation in [
            lambda: account.deposit_withdraw(1, receiving_days=1),
            lambda: account.finance_repay(100),
        ]:
            try:
                operation()
            except RuntimeError:
                pass
            else:
                raise AssertionError("UNSUPPORTED_CASH_OPERATION_ACCEPTED")
            assert account.get_state() == before

    def state(self):
        value = super().state()
        account = self.env.portfolio.stock_account
        value.update(account.cash_projection())
        value["cash_account_state"] = account.get_state()["aq_cash"]
        return value

    def trade(self, event):
        super().trade(event)
        self.last_trade = event.trade

    def repeat_settlement(self, event):
        account = self.env.portfolio.stock_account
        before = copy.deepcopy(account.get_state())
        account._on_settlement(event)
        if hasattr(self, "last_trade"):
            account.apply_trade(self.last_trade)
            different = copy.copy(self.last_trade)
            different._price += 1
            try:
                account.apply_trade(different)
            except RuntimeError as exc:
                assert str(exc) == "CASH_EXECUTION_ID_COLLISION"
            else:
                raise AssertionError("CHANGED_DUPLICATE_TRADE_ACCEPTED")
        assert account.get_state() == before


def main():
    import rqalpha.main
    import rqalpha.utils.config as configs

    import rqprobe.mod as mod

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--mode",
        choices=["cash", "reuse", "partial", "dividend", "dividend_tax", "withdrawal_tail"],
        default="cash",
    )
    parser.add_argument("--checkpoint")
    args = parser.parse_args()
    out = Path(args.out).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    provider_type = Provider
    if args.mode in {"dividend", "dividend_tax"}:
        from dividend_fixture import FACT, Frame

        import ashare_quant.round_one.carry_contract as contract
        from rqprobe.fixture import aware

        fact = {
            **FACT,
            "record_date": "2024-01-04",
            "ex_date": "2024-01-05",
            "payment_date": "2024-01-05",
        }

        class DividendFrame(Frame):
            def corporate_actions(self, kind):
                if kind == "split":
                    return None
                from rqalpha.environment import Environment

                return contract.admit_dividend_execution(
                    fact,
                    security=SID,
                    as_of=self.as_of,
                    next_session=Environment.get_instance().data_proxy.get_next_trading_date,
                    model=json.loads(
                        Path(contract.__file__).with_name("dividend-model.json").read_text()
                    ),
                )

        class DividendProvider(Provider):
            def at(self, at):
                return DividendFrame(self, aware(at))

        provider_type = DividendProvider
    mod.SyntheticProvider = provider_type
    mod.ProbeMod = CashProbe
    records = provider_type().records("1m", start="2024-01-04", end="2024-01-08")
    events_path = out.with_suffix(".events.json")
    events_path.write_text(json.dumps(records))
    strategy = "cash_tail_strategy.py" if args.mode == "withdrawal_tail" else "cash_strategy.py"
    strategy_mode = "dividend" if args.mode == "dividend_tax" else args.mode
    source = f"MODE={strategy_mode!r}\n" + Path(__file__).with_name(strategy).read_text()
    requested = {
        "base": dict(
            start_date="2024-01-04",
            end_date="2024-01-08",
            frequency="1m",
            accounts={"stock": 2000 if args.mode == "reuse" else 100000},
            run_type="b",
            rqdatac_uri="disabled",
            auto_update_bundle=False,
            data_bundle_path=str(out.parent / "ABSENT"),
            persist=bool(args.checkpoint),
            persist_mode="real_time",
            capital_gain_tax_rate=0,
        ),
        "extra": dict(log_level="error", locale="en_US"),
        "mod": dict(
            sys_progress={"enabled": False},
            sys_analyser={"enabled": False},
            sys_simulation=dict(matching_type="current_bar", volume_percent=0.25),
            sys_accounts=dict(
                dividend_tax_enabled=False, dividend_reinvestment=False, stock_t1=True
            ),
            probe=dict(
                enabled=True,
                lib="rqprobe.mod",
                priority=1000,
                fixed_dividend_tax=args.mode == "dividend_tax",
                actions=False,
                missing=[],
                tplus=1,
                recorded=True,
                result=str(out),
                events_path=str(events_path),
                checkpoint=args.checkpoint,
                identity="",
            ),
        ),
    }
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


if __name__ == "__main__":
    main()
