"""Explicit cancel-and-replace target policy. Not a JoinQuant equivalence claim."""
def target_shares(sid, quantity, *, style=None):
    from rqalpha.api import get_open_orders, cancel_order, order_to
    for order in list(get_open_orders()):
        if order.order_book_id == sid:
            cancel_order(order)
    # Native current position, cash, validation and costs remain authoritative.
    # Cancelling a partly filled order can incur another minimum commission.
    result = order_to(sid,quantity,style=style)
    if not isinstance(result,list): raise RuntimeError('UNSUPPORTED_RQ_ORDER_TO_RESULT')
    return result
