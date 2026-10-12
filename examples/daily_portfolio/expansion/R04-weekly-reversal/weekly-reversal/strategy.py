from ashare_quant.daily_portfolio.signals import decide

def on_decision(history, prior_equity):
    return decide('weekly-reversal', history, prior_equity)
