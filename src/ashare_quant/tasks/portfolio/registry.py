"""Portfolio algorithm versions in the existing immutable strategy registry table."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, digest, regular, require, sha
from ashare_quant.tasks.portfolio.contracts import PortfolioStrategyVersion

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
    return regular(Path(__file__).parents[2] / "portfolio/signals.py")


class Registry:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def decode(raw, expected_version_id):
        try:
            version = PortfolioStrategyVersion.model_validate_json(raw)
        except (ValueError, TypeError) as exc:
            raise DomainError(
                "PORTFOLIO_STRATEGY_CONFLICT", "Stored strategy version is malformed", 409
            ) from exc
        require(
            version.version_id == expected_version_id,
            "PORTFOLIO_STRATEGY_CONFLICT",
            "Requested strategy ID differs from its frozen version body",
        )
        require(
            version.source_sha256 == sha(source()),
            "PORTFOLIO_STRATEGY_CHANGED",
            "Use the strategy version's original installed algorithm",
        )
        require(
            version.parameter_sha256 == digest(version.definition)
            and version.version_id
            == digest(
                {
                    "api": "portfolio-strategy.v1",
                    "definition": version.definition.model_dump(mode="json"),
                    "source_sha256": version.source_sha256,
                }
            ),
            "PORTFOLIO_STRATEGY_CONFLICT",
            "Strategy definition differs from its immutable identity",
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
                "INSERT OR IGNORE INTO registered_strategies VALUES (?,?)",
                (version.version_id, canonical(version).decode()),
            )
        return self.get(version.version_id)

    def get(self, version_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT version_id,body FROM registered_strategies WHERE version_id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise DomainError(
                "PORTFOLIO_STRATEGY_NOT_FOUND", "Register a portfolio strategy version", 404
            )
        return self.decode(row[1], version_id)

    def list(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT version_id,body FROM registered_strategies ORDER BY version_id"
            ).fetchall()
        result = []
        for key, raw in rows:
            try:
                template = json.loads(raw)["definition"]["template"]
            except (ValueError, KeyError, TypeError) as exc:
                raise DomainError(
                    "PORTFOLIO_STRATEGY_CONFLICT", "Stored strategy record is malformed", 409
                ) from exc
            if template == "daily_portfolio.v1":
                result.append(self.decode(raw, key))
        return tuple(result)


def strategy_files(request, strategy, account_id):
    """Derive the exact executable bytes from the requested immutable version."""
    Registry.decode(canonical(strategy), request.strategy_version_id)
    execution = execution_manifest(request, strategy)
    execution["task_account_id"] = account_id
    algorithm = strategy.definition.algorithm
    return {
        "execution.json": canonical(execution),
        "strategy.json": canonical(strategy),
        algorithm + "/config.json": canonical(execution["strategies"][0]),
        algorithm + "/strategy.py": (
            "from ashare_quant.portfolio.signals import decide\n"
            "def on_decision(history, prior_equity):\n"
            f"    return decide({algorithm!r}, history, prior_equity)\n"
        ).encode(),
    }


def check_frozen_strategy(manifest, captured):
    """Bind request, frozen executable, prepared session and published receipt."""
    files = strategy_files(
        manifest.request, manifest.strategy, "bt-" + manifest.job_id.removeprefix("m2job-")
    )
    require(
        all(captured.get(name) == raw for name, raw in files.items()),
        "PORTFOLIO_FROZEN_STRATEGY_CONFLICT",
        "Frozen strategy or executable bytes differ from the requested version",
    )
    algorithm = manifest.strategy.definition.algorithm
    details = manifest.session["details"]
    require(
        details["strategy"] == algorithm
        and details["manifest"] == json.loads(files["execution.json"])
        and details["strategy_source_sha256"] == sha(files[algorithm + "/strategy.py"]),
        "PORTFOLIO_FROZEN_STRATEGY_CONFLICT",
        "Prepared native session differs from the requested executable",
    )


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
