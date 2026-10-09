"""Hand-computable research cases. All prices, dates and rules are synthetic."""

from .contracts import EngineIdentity
from .data import RQALPHA_COMMIT

RQ_IDENTITY = EngineIdentity(engine_id="rqalpha", version="6.4.1",
                            source_commit=RQALPHA_COMMIT, is_test_double=False)


def rq_specifications():
    values = {
        "buy_hold": ("1d", "8995.00", "10095.00", 100, 1),
        "t_plus_one": ("1m", "10089.45", "10089.45", 0, 2),
        "insufficient_cash": ("1d", "500.00", "500.00", 0, 0),
        "partial_fill": ("1m", "8995.00", "9995.00", 100, 1),
        "cross_day": ("1m", "9889.55", "9889.55", 0, 2),
        "corporate_action": ("1m", "9095.00", "9995.00", 100, 1),
        "restart": ("1m", "8995.00", "10095.00", 100, 1),
    }
    return {name: {
        "schema_version": 1, "evidence_kind": "synthetic",
        "parameters": {"initial_cash": 500 if name == "insufficient_cash" else 10000,
                       "security": "600000.XSHG", "quantity": 300 if name == "partial_fill" else 100,
                       "frequency": freq, "start_date": "2024-01-02", "end_date": "2024-01-04",
                       "matching": "current_bar", "volume_percent": "0.25",
                       "minimum_commission": "5.00", "sell_tax_rate": "0.0005"},
        "expected": {"cash": cash, "equity": equity, "quantity": qty, "fill_count": fills,
                     "remaining_order_status": "rejected" if name == "partial_fill" else None},
        "data": "synthetic native-account fixture; not A数达 or real market validation",
        "real_market_validation": "NOT_RUN",
    } for name, (freq, cash, equity, qty, fills) in values.items()}
