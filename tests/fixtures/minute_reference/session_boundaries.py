"""Real AD boundary regressions; this is not a captured JQ reference strategy."""

# ruff: noqa: F405

from aquant import *  # noqa: F403


def initialize(context):
    g.stocks = ["600000.XSHG", "000001.XSHE"]
    g.zero_checks = 0
    run_daily(opening, "09:30")
    run_daily(resting, "14:58")
    run_daily(zero_minute, "14:59")


def opening(context):
    for sid in g.stocks:
        frame = attribute_history(sid, 2, "1m", ["high", "volume"], fq=None)
        assert all(index.date() < context.current_dt.date() for index in frame.index)
        assert get_current_data()[sid].day_open > 0
        try:
            get_price(
                sid,
                start_date=context.current_dt.date(),
                end_date=context.current_dt.date(),
                fields=["volume"],
                fq=None,
            )
        except ValueError as exc:
            assert "INSUFFICIENT" in str(exc)
        else:
            raise AssertionError("Today's final volume was visible at opening")
    g.old = {}


def resting(context):
    for sid in g.stocks:
        limit = round(get_current_data()[sid].last_price + 0.1, 2)
        g.old[sid] = order(sid, 100000, LimitOrderStyle(limit))


def zero_minute(context):
    g.filled = {}
    for sid in g.stocks:
        assert attribute_history(sid, 1, "1m", ["volume"], fq=None).volume.iloc[0] == 0
        old = get_orders(g.old[sid].order_id)[g.old[sid].order_id]
        g.filled[sid] = old.filled
        market = order(sid, 100)
        assert market.filled == 0 and market.status == OrderStatus.canceled
        pending = order(
            sid, 100, LimitOrderStyle(round(get_current_data()[sid].last_price + 0.1, 2))
        )
        assert pending.filled == 0 and pending.order_id in get_open_orders()


def handle_data(context, data):
    if context.current_dt.strftime("%H:%M") != "14:59":
        return
    for sid in g.stocks:
        assert get_orders(g.old[sid].order_id)[g.old[sid].order_id].filled == g.filled[sid]
        assert not any(
            t.security == sid and t.time == context.current_dt for t in get_trades().values()
        )
        g.zero_checks += 1
    record(real_zero_rows_checked=g.zero_checks)


def on_strategy_end(context):
    assert g.zero_checks == 4
    assert get_orders() == get_trades() == {}
    record(real_boundary_complete=1)
