"""Original acceptance implementation of a common mathematical rule; no external code copied."""
from ashare_quant.daily_portfolio.signals import decide

def on_decision(history, prior_equity):
    return decide('momentum-low-volatility', history, prior_equity)
