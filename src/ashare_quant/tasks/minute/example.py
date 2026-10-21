"""An editable example, served as source; never imported as a strategy."""

SOURCE = """from jqdata import *

# 参数来自右侧 JSON；股票池和日期来自本次配置。
def initialize(context):
    g.securities = list(context.securities)
    g.day = 0
    set_universe(g.securities)
    run_daily(select_stock, time="09:50")
    run_daily(exit_positions, time="14:40")


def before_trading_start(context):
    g.day += 1


def select_stock(context):
    if g.day != 1 or context.current_dt.date() == context.run_params.end_date:
        return
    lookback = int(context.parameters.get("lookback", 5))
    scores = {}
    for security in g.securities:
        values = attribute_history(security, lookback, "1m", ["close"], df=False, fq=None)["close"]
        if len(values) >= lookback:
            scores[security] = float(values[-1] / values[0] - 1)
    if scores:
        chosen = max(scores, key=scores.get)
        shares = int(context.parameters.get("shares", 100))
        order_target(chosen, shares)
        log.info("选择", chosen, "目标股数", shares)
        record(momentum=scores[chosen])


def exit_positions(context):
    # 当前模型要求在结束日收盘前平仓，保留 T+1 约束。
    if context.current_dt.date() == context.run_params.end_date:
        for security in g.securities:
            order_target(security, 0)
"""
