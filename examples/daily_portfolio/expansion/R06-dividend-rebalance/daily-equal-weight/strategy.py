from ashare_quant.daily_portfolio.signals import decide

def on_decision(history, prior_equity):
    return decide('daily-equal-weight', history, prior_equity)
