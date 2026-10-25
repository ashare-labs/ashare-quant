"""Synthetic strategy sources for tests; never served as a product default."""

SOURCE = """from aquant import *

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
    # 本测试策略选择在结束日平仓；产品允许保留期末持仓，T+1 约束仍适用。
    if context.current_dt.date() == context.run_params.end_date:
        for security in g.securities:
            order_target(security, 0)
"""

DAILY_SOURCE = '''from aquant import *

def initialize(context):
    set_universe(context.securities)
    run_daily(rebalance, time="open")

def rebalance(context):
    lookback = int(context.parameters.get("lookback", 20))
    shares = int(context.parameters.get("shares", 100))
    for security in context.securities:
        prices = attribute_history(security, lookback, "1d", ["close"], fq=None)
        target = shares if prices.close.iloc[-1] > prices.close.mean() else 0
        order_target(security, target)
        log.info(security, "上一交易日", str(prices.index[-1].date()), "目标股数", target)
        record(close=float(prices.close.iloc[-1]), average=float(prices.close.mean()))
'''
