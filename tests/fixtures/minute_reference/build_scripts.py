"""Freeze observable copies of the two research scripts, without replacing their decisions.

Only assertions become recorded predicates so a platform difference does not
prevent later original orders from being observed. Calls are evaluated once.
The generated business AST is checked in tests; original examples stay intact.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = ("shared_cash_t1", "visible_momentum")
HELPERS = """
from jqdata import *
import json
import math

def _clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):
        return _clean(value.item())
    return str(value)

def _fields(obj, names):
    result = {}
    for name in names.split():
        try:
            result[name] = _clean(getattr(obj, name))
        except Exception as exc:
            result[name] = {"unavailable": type(exc).__name__}
    return result

def _emit(context, kind, **values):
    g._sequence += 1
    row = dict(seq=g._sequence, strategy=CASE, time=str(context.current_dt), kind=kind, **values)
    log.info("AQMIN_JSON " + json.dumps(_clean(row), sort_keys=True, allow_nan=False))

def _snapshot(context, label):
    # Repeated checkpoints need account/position clocks, not copies of every
    # prior order. Keep the free platform log small enough to include its tail.
    include_orders = not label.startswith("handle_data:")
    _emit(context, "snapshot", label=label, day=getattr(g, "day", None),
        account=_fields(context.portfolio, "starting_cash available_cash locked_cash positions_value total_value"),
        positions=[dict(security=s, **_fields(p, "total_amount closeable_amount today_amount avg_cost price value"))
                   for s, p in sorted(context.portfolio.positions.items())],
        orders=[_fields(o, "order_id security amount filled is_buy status price avg_cost commission add_time")
                for o in get_orders().values()] if include_orders else None,
        open_orders=[str(k) for k in get_open_orders()],
        trades=[_fields(t, "trade_id order_id security amount price time at side commission tax")
                for t in get_trades().values()] if include_orders else None)

def _observe(context, label, value):
    _emit(context, "assertion_observation", label=label, passed=bool(value))

def _submit(context, api, security, value, *args, **kwargs):
    _emit(context, "submit", api=api, security=security, value=value,
          style=_fields(args[0], "price _limit_price") if args else None,
          market=_fields(get_current_data()[security], "last_price paused is_st high_limit low_limit day_open"))
    _snapshot(context, "before:" + api + ":" + security)
    functions = {"order": order, "order_value": order_value, "order_target": order_target}
    result = functions[api](security, value, *args, **kwargs)
    _emit(context, "order_return", api=api, security=security, returned_none=result is None,
          order=None if result is None else _fields(result, "order_id security amount filled is_buy status price avg_cost commission add_time"))
    _snapshot(context, "after:" + api + ":" + security)
    return result

def _history(context, *args, **kwargs):
    frame = history(*args, **kwargs)
    _emit(context, "signal_history", labels=[str(i) for i in frame.index],
          close={s: [float(v) for v in frame[s]] for s in g.stocks},
          scores={s: float(frame[s].iloc[-1] / frame[s].iloc[0] - 1) for s in g.stocks})
    return frame

def _checkpoint(context):
    _snapshot(context, "checkpoint")

def handle_data(context, data):
    # Observe lifecycle coverage without requesting any additional source data.
    clock = context.current_dt.strftime("%H:%M")
    if clock in ("09:35", "09:40", "09:41", "09:45", "09:50", "09:51", "11:29", "11:30", "12:00", "13:00", "13:01", "13:10", "13:11", "14:56"):
        _snapshot(context, "handle_data:" + clock)

def on_strategy_end(context):
    # JQ's terminal callback: independent of after-close record() chart output.
    _snapshot(context, "strategy_end")
    _emit(context, "complete", last_snapshot_sequence=g._sequence)
"""


class Observe(ast.NodeTransformer):
    def visit_Assert(self, node):
        return ast.Expr(
            ast.Call(
                ast.Name("_observe", ast.Load()),
                [
                    ast.Name("context", ast.Load()),
                    ast.Constant(f"line:{node.lineno}:{ast.unparse(node.test)}"),
                    self.visit(node.test),
                ],
                [],
            )
        )

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Name):
            if node.func.id in {"order", "order_value", "order_target"}:
                node.args = [
                    ast.Name("context", ast.Load()),
                    ast.Constant(node.func.id),
                    *node.args,
                ]
                node.func = ast.Name("_submit", ast.Load())
            elif node.func.id == "history":
                node.func = ast.Name("_history", ast.Load())
                node.args.insert(0, ast.Name("context", ast.Load()))
        return node


def generate(name, platform):
    if name not in NAMES or platform not in ("aq", "jq"):
        raise ValueError("Only the two frozen cases and aq/jq platforms are supported")
    original = (ROOT / "tests/fixtures/research/minute_research" / f"{name}.py").read_text()
    tree = Observe().visit(ast.parse(original))
    # JQ injects trading functions/styles into the strategy global namespace;
    # jqdata itself does not export LimitOrderStyle. AQ also injects this subset.
    # The common wildcard data import in HELPERS is accepted by both platforms.
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "jqdata")
    ]
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "initialize":
            settings = """
g._sequence = 0
set_option("use_real_price", True)
set_option("avoid_future_data", True)
set_order_cost(OrderCost(open_tax=0, close_tax=0.0005, open_commission=0.0003, close_commission=0.0003, close_today_commission=0, min_commission=5), type="stock")
set_slippage(FixedSlippage(0), type="stock")
"""
            if platform == "jq":
                settings += 'set_option("order_volume_ratio", 0.25)\n'
            node.body = (
                ast.parse(settings).body
                + node.body
                + ast.parse(
                    '_emit(context, "identity", case=CASE, platform=PLATFORM, source_sha256=SOURCE_SHA256, '
                    'stocks=g.stocks, window=["2026-10-09", "2026-10-12"], initial_cash=100000, '
                    "commission_rate=0.0003, min_commission=5, sell_tax=0.0005, slippage=0, volume_ratio=0.25, "
                    'assertions="observed_without_abort", run_params=_fields(context.run_params, "type frequency start_date end_date"))'
                ).body
            )
        else:
            node.body = ast.parse(
                f'_emit(context, "callback", name={node.name!r}, edge="enter")'
            ).body + [
                ast.Try(
                    body=node.body,
                    handlers=[],
                    orelse=[],
                    finalbody=ast.parse(f'_snapshot(context, "callback_exit:{node.name}")').body,
                )
            ]
    ast.fix_missing_locations(tree)
    return (
        "# Generated by build_scripts.py. Original assertions are observations; trading decisions are unchanged.\n"
        + f"CASE = {name!r}\nPLATFORM = {platform!r}\n"
        + f"SOURCE_SHA256 = {hashlib.sha256(original.encode()).hexdigest()!r}\n"
        + HELPERS
        + "\n"
        + ast.unparse(tree)
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"baseline": "14d8dfe7d4b79343162a5f3877613bdd44ac58b3", "scripts": {}}
    for name in NAMES:
        for platform in ("aq", "jq"):
            path = args.output / f"{name}.{platform}.py"
            path.write_text(generate(name, platform))
            manifest["scripts"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
