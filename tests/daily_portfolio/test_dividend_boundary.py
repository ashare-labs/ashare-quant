from types import SimpleNamespace


def test_cash_entitlement_is_not_the_ex_price_reference_deduction():
    """A buyback exclusion can make these two amounts differ; keep their roles."""
    from ashare_quant.daily_portfolio.native import Source

    security = "600000.XSHG"
    action = {
        "security": security,
        "kind": "cash_dividend",
        "record_date": "2020-02-04",
        "ex_date": "2020-02-05",
        "pay_date": "2020-02-07",
        "cash_per_share": "1",
        "source_event": {"ex_price_cash_deduction": "0.936124", "payment_time": None},
    }
    source = Source(SimpleNamespace(data=SimpleNamespace(ids=[], actions=[action])))
    instrument = SimpleNamespace(order_book_id=security)
    rows = source.get_dividend(instrument)
    assert len(rows) == 1
    assert rows[0]["dividend_cash_before_tax"] / rows[0]["round_lot"] == 1
    assert rows[0]["book_closure_date"] == 20200204
    assert rows[0]["ex_dividend_date"] == 20200205
    assert rows[0]["payable_date"] == 20200207
    action["source_event"]["ex_price_cash_deduction"] = "0.75"
    assert (source.get_dividend(instrument) == rows).all()
    assert source.get_dividend(SimpleNamespace(order_book_id="000001.XSHE")) is None
