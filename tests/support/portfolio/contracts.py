"""The portfolio contract is separate from the unchanged single-security v6 API."""

from datetime import date as CalendarDate
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from ashare_quant.contracts import Contract, Money
from ashare_quant.market.contracts import (
    PortfolioDataset,
    PortfolioPreflight,
    PortfolioScope,
    PortfolioSelection,
)
from ashare_quant.tasks.policy import ServiceStatus
from tests.support.daily.contracts import JobID, JobView, ResultReference
from tests.support.daily.result_contracts import JSONObject
from tests.support.legacy_backtests.contracts import Sha

Security = Annotated[str, Field(pattern=r"^[0-9]{6}\.(XSHG|XSHE)$")]
Algorithm = Literal[
    "equal-weight",
    "daily-equal-weight",
    "momentum",
    "trend",
    "weekly-reversal",
    "low-volatility",
    "momentum-low-volatility",
]


class PortfolioStrategyDefinition(Contract):
    template: Literal["daily_portfolio.v1"] = "daily_portfolio.v1"
    algorithm: Algorithm


class PortfolioStrategyVersion(Contract):
    version_id: Sha
    definition: PortfolioStrategyDefinition
    source_sha256: Sha
    parameter_sha256: Sha
    created_at: AwareDatetime




class PortfolioCreate(PortfolioSelection):
    contract_version: Literal["portfolio-jobs.v1"] = "portfolio-jobs.v1"
    name: str = Field(min_length=1, max_length=80, pattern=r"^\S(?:[^\r\n]*\S)?$")
    idempotency_key: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    strategy_version_id: Sha
    initial_cash: Money = "1000000.00"
    fee_model: Literal["fixed-commission-tax.v1"] = "fixed-commission-tax.v1"
    volume_ratio: Annotated[str, Field(pattern=r"^(0\.[0-9]+|1(?:\.0+)?)$")] | None = None
    assumption_ack_sha256: Sha

    @field_validator("initial_cash", "volume_ratio")
    @classmethod
    def positive(cls, value):
        if value is not None and Decimal(value) <= 0:
            raise ValueError("Amount must be positive")
        return value

    @property
    def window_id(self):
        return "selected"


class PortfolioIdentity(Contract):
    backend_version: str
    source_sha256: Sha
    contract_version: Literal["portfolio-jobs.v1"] = "portfolio-jobs.v1"
    independent_review_status: Literal["not_reviewed"] = "not_reviewed"








class PortfolioCatalogue(Contract):
    contract_version: Literal["portfolio-jobs.v1"] = "portfolio-jobs.v1"
    service_identity: PortfolioIdentity
    native_ready: bool
    native_error: str | None
    datasets: tuple[PortfolioDataset, ...]
    fee_models: tuple[JSONObject, ...]
    strategy_algorithms: tuple[str, ...]
    preflight_required: Literal[True] = True
    dataset_registration: Literal["local_cli_or_product_publication"] = "local_cli_or_product_publication"
    python_registration: Literal["not_supported_use_registered_algorithms"] = (
        "not_supported_use_registered_algorithms"
    )
    account_policy: Literal["new_isolated_account_per_job"] = "new_isolated_account_per_job"
    instrument_scope: Literal["main_board_ordinary_a_shares"] = "main_board_ordinary_a_shares"


class PortfolioManifest(Contract):
    schema_version: Literal["portfolio-job-manifest.v1"] = "portfolio-job-manifest.v1"
    job_id: JobID
    request: PortfolioCreate
    service_identity: PortfolioIdentity
    execution_scope: PortfolioScope
    strategy: PortfolioStrategyVersion
    data_identity: PortfolioDataset
    preflight: PortfolioPreflight
    session: JSONObject
    session_sha256: Sha
    session_file_sha256: Sha
    prepared_proof: JSONObject
    files: dict[str, Sha]


class PortfolioJobView(JobView):
    contract_version: Literal["portfolio-jobs.v1"] = "portfolio-jobs.v1"
    request: PortfolioCreate


class PortfolioJobList(Contract):
    items: tuple[PortfolioJobView, ...]


class PortfolioPosition(Contract):
    quantity: int
    sellable: int
    price: str
    value: str
    average_price: str
    dividend_receivable: str
    restricted_shares: int = 0
    pending_shares: int = 0
    pending_share_value: str = "0"


class PortfolioAccount(Contract):
    date: CalendarDate
    cash: str
    equity: str
    positions: dict[Security, PortfolioPosition]
    at: AwareDatetime | None = None


class PortfolioDay(PortfolioAccount):
    at: AwareDatetime
    day_fee: str
    cumulative_fee: str


class PortfolioTrade(Contract):
    id: str
    order_id: str
    date: CalendarDate
    at: AwareDatetime
    security: Security
    side: Literal["BUY", "SELL"]
    quantity: int
    price: str
    commission: str
    tax: str


class PortfolioOrder(Contract):
    id: str
    security: Security
    date: CalendarDate
    at: AwareDatetime | None
    side: Literal["BUY", "SELL"]
    quantity: int
    filled: int
    status: Literal["FILLED", "CANCELLED", "REJECTED"]
    message: str


class PortfolioSignal(Contract):
    selected: tuple[Security, ...]
    scores: dict[Security, str]
    weights: dict[Security, str]
    targets: dict[Security, int]


class PortfolioDecision(Contract):
    date: CalendarDate
    at: AwareDatetime
    cutoff: AwareDatetime
    history_sha256: Sha
    prior_account_sha256: Sha
    rebalance: bool
    signal: PortfolioSignal | None


class PortfolioSummary(Contract):
    initial_cash: str
    final_cash: str
    final_equity: str
    net_change: str
    net_return: str
    max_drawdown: str
    commission_total: str
    tax_total: str
    fee_total: str
    trade_count: int
    order_count: int
    benchmark: None = None
    annualized_return: None = None
    sharpe: None = None
    metric_scope: str


class PortfolioResult(Contract):
    contract_version: Literal["portfolio-result.v1"] = "portfolio-result.v1"
    job_id: JobID
    account_id: str
    request: PortfolioCreate
    strategy: PortfolioStrategyVersion
    scope: PortfolioScope
    service_identity: PortfolioIdentity
    data_identity: PortfolioDataset
    summary: PortfolioSummary
    account: PortfolioAccount
    days: tuple[PortfolioDay, ...]
    trades: tuple[PortfolioTrade, ...]
    orders: tuple[PortfolioOrder, ...]
    decisions: tuple[PortfolioDecision, ...]
    receipt: JSONObject
    audit: JSONObject
    recovery: JSONObject
    assumptions: tuple[JSONObject | str, ...]
    limitations: tuple[str, ...]
    economic_scope: str
    historical_pit_verified: Literal[False] = False
    result_status: Literal["completed_not_independently_reviewed"] = (
        "completed_not_independently_reviewed"
    )


class PortfolioResultReference(ResultReference):
    pass


class PortfolioServiceStatus(ServiceStatus):
    job_contract_version: Literal["portfolio-jobs.v1"] = "portfolio-jobs.v1"
    counts_scope: Literal["shared_v6_and_portfolio_queue"] = "shared_v6_and_portfolio_queue"
