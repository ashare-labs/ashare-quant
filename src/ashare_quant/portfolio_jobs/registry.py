"""Portfolio algorithm versions in the existing immutable strategy registry table."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import canonical, digest, regular, require, sha

from .contracts import PortfolioStrategyVersion

ALGORITHMS = (
    "equal-weight",
    "daily-equal-weight",
    "momentum",
    "trend",
    "weekly-reversal",
    "low-volatility",
    "momentum-low-volatility",
)
FEES = {
    "buy_commission_rate": "0.0003",
    "sell_commission_rate": "0.0003",
    "minimum_commission": "5.00",
    "sell_tax_rate": "0.001",
    "transfer_fee": "0",
    "slippage": "0",
    "scope": "fixed acceptance cost model for all windows, not a statement of historical statutory rates",
}


def source():
    return regular(Path(__file__).parents[1] / "daily_portfolio/signals.py")


class Registry:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def decode(raw):
        version = PortfolioStrategyVersion.model_validate_json(raw)
        require(
            version.source_sha256 == sha(source()),
            "PORTFOLIO_STRATEGY_CHANGED",
            "Use the strategy version's original installed algorithm",
        )
        require(version.parameter_sha256 == digest(version.definition))
        require(
            version.version_id
            == digest(
                {
                    "api": "portfolio-strategy.v1",
                    "definition": version.definition.model_dump(mode="json"),
                    "source_sha256": version.source_sha256,
                }
            )
        )
        return version

    def register(self, definition):
        version = PortfolioStrategyVersion(
            version_id=digest(
                {
                    "api": "portfolio-strategy.v1",
                    "definition": definition.model_dump(mode="json"),
                    "source_sha256": sha(source()),
                }
            ),
            definition=definition,
            source_sha256=sha(source()),
            parameter_sha256=digest(definition),
            created_at=datetime.now(timezone.utc),
        )
        with self.store.connection(transaction=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO m2_strategies VALUES (?,?)",
                (version.version_id, canonical(version).decode()),
            )
        return self.get(version.version_id)

    def get(self, version_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT body FROM m2_strategies WHERE version_id=?", (version_id,)
            ).fetchone()
        if row is None or json.loads(row[0])["definition"]["template"] != "daily_portfolio.v1":
            raise DomainError(
                "PORTFOLIO_STRATEGY_NOT_FOUND", "Register a portfolio strategy version", 404
            )
        return self.decode(row[0])

    def list(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT body FROM m2_strategies WHERE json_extract(body, '$.definition.template')='daily_portfolio.v1' ORDER BY version_id"
            ).fetchall()
        return tuple(self.decode(row[0]) for row in rows)


def execution_manifest(request, strategy):
    return {
        "schema": "aq-real-strategies.v1",
        "securities": list(request.securities),
        "windows": [
            {"id": "selected", "start": str(request.start_date), "end": str(request.end_date)}
        ],
        "strategies": [{"id": strategy.definition.algorithm}],
        "execution": {
            "initial_cash": request.initial_cash,
            "fees": FEES,
            "volume_ratio": request.volume_ratio,
            "unfilled": "native market remainder cancelled during execution; next scheduled rebalance recomputes",
            "corporate_action_policy": "source covered gross cash and supported shares; price-only mode excludes actions",
        },
    }
