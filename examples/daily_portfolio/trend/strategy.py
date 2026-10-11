"""Frozen acceptance question; only previous-session closes and equity are inputs."""
from ashare_quant.daily_portfolio.signals import decide


def on_decision(history, prior_equity):
    return decide('trend', history, prior_equity)
