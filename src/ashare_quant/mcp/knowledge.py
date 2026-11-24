"""Static strategy analyzer, aquant API reference resources, and workflow prompts."""

import ast
import re
from typing import Any

from ashare_quant.mcp.contracts import normalize_security

AQUANT_SUPPORTED_FUNCTIONS = {
    "run_daily",
    "set_universe",
    "set_benchmark",
    "set_option",
    "set_order_cost",
    "set_slippage",
    "get_current_data",
    "attribute_history",
    "history",
    "get_bars",
    "get_price",
    "order",
    "order_target",
    "order_value",
    "order_target_value",
    "get_orders",
    "get_open_orders",
    "cancel_order",
    "get_trades",
    "record",
    "get_all_securities",
}
AQUANT_SUPPORTED_OBJECTS = {
    "g",
    "log",
    "LimitOrderStyle",
    "MarketOrderStyle",
    "OrderCost",
    "FixedSlippage",
    "OrderStatus",
}
AQUANT_SUPPORTED = AQUANT_SUPPORTED_FUNCTIONS | AQUANT_SUPPORTED_OBJECTS

AQUANT_UNSUPPORTED_HINTS = {
    "get_fundamentals": "Fundamental SQLAlchemy queries are not supported in aquant callbacks; pass pre-screened securities or parameters.",
    "query": "ORM query() is not supported; use attribute_history / history / get_price for price/volume data.",
    "valuation": "ORM valuation table is not supported in aquant.",
    "indicator": "ORM indicator table is not supported in aquant.",
    "income": "ORM income table is not supported in aquant.",
    "balance": "ORM balance table is not supported in aquant.",
    "cash_flow": "ORM cash_flow table is not supported in aquant.",
    "get_index_stocks": "get_index_stocks is not supported; pass explicit securities or use get_all_securities(['stock'], date=context.previous_date).",
    "get_industry_stocks": "get_industry_stocks is not supported; pass explicit securities in parameters or securities=[...].",
    "get_security_info": "get_security_info is not supported; use get_all_securities(['stock'], date=context.previous_date).",
    "get_trade_days": "get_trade_days is not supported; use attribute_history / get_price date indexes.",
    "get_extras": "get_extras is not supported; use get_current_data()[security].is_st for ST status.",
    "get_factor_values": "get_factor_values is not supported inside aquant; use run_factor() for factor calculation and evaluation.",
    "run_weekly": "run_weekly is not supported; use run_daily(fn, time='open') and check context.current_dt.weekday().",
    "run_monthly": "run_monthly is not supported; use run_daily(fn, time='open') and track month changes on g.",
    "set_subportfolios": "Multiple subportfolios are not supported.",
    "set_data_option": "set_data_option is not supported.",
    "read_file": "Filesystem read_file is not supported; use multi-file project modules or parameters.",
    "write_file": "Filesystem write_file is not supported; use log.info(...) or record(...).",
    "send_message": "send_message is not supported; use log.info(...) or record(...).",
    "enable_profile": "enable_profile is not supported.",
}

LEGACY_CODE_LITERAL_RE = re.compile(
    r"^(?:[0-9]{6}\.(?:SZ|SH|SS)|(?:SZ|SH|SS)\.?[0-9]{6})$", re.IGNORECASE
)

API_REFERENCE_MD = """# `aquant` Strategy API Reference

`aquant` is bound automatically inside the backtest worker process (`from aquant import *`).
**Never** import `jqdata` (`legacy_module_supported=False`).

## 1. Lifecycle & Scheduling

- **`def initialize(context):`** *(required in entrypoint)*
  Called once at strategy start. Configure benchmark, universe, parameters, `g.*` state, and schedule callbacks here.
- **`run_daily(func, time="open", reference_security=None)`**
  Schedule `func(context)` daily. Supported `time` values:
  - `"before_open"` (`09:00`)
  - `"open"` (`09:30` in `1d` mode; first bar in `1m` mode)
  - `"every_bar"` (every bar in the session)
  - `"after_close"` (`15:30`)
  - Exact intraday time `"HH:MM"` (e.g. `"09:50"`, `"14:50"`, in `1m` mode)
- Optional direct callbacks: `before_trading_start(context)`, `handle_data(context, data)`, `after_trading_end(context)`, `on_strategy_end(context)`.

## 2. Market Data & Universe

- **`attribute_history(security, count, unit="1d", fields=["open", "close", "high", "low", "volume", "money"], skip_paused=True, df=True, fq="pre")`**
  Returns trailing history for a single security before current time. Supports `fq="pre"` (default), `fq="post"`, and `fq=None` (raw prices).
- **`history(count, unit="1d", field="close", security_list=None, df=True, skip_paused=False, fq="pre")`**
  Cross-sectional history panel across multiple securities aligned on the trailing `count` visible trading sessions; newly listed or missing sessions return `NaN` without aborting the run.
- **`get_price(security, start_date=None, end_date=None, frequency="1d", fields=None, skip_paused=False, fq="pre", count=None)`**
- **`get_bars(security, count, unit="1d", fields=("date", "open", "high", "low", "close", "volume", "money"), include_now=False, end_dt=None, fq_ref_date=None)`**
- **`get_current_data()`**
  Mapping indexed by `security` (`current = get_current_data()[security]`):
  - `current.last_price`, `current.day_open`, `current.high_limit`, `current.low_limit`, `current.paused`, `current.is_st`
- **`get_all_securities(types=["stock"], date=None)`**
  Returns a DataFrame indexed by canonical security code (`000001.XSHE`, `600000.XSHG`).
- **`set_universe(security_list)`** / **`set_benchmark("000300.XSHG" | None)`**

## 3. Ordering & Portfolio

- **`order(security, amount, style=None)`** — buy/sell `amount` shares (rounded to 100-share lots on buys).
- **`order_target(security, amount, style=None)`** — adjust position to target shares.
- **`order_value(security, value, style=None)`** — buy/sell by cash value (CNY).
- **`order_target_value(security, value, style=None)`** — adjust position to target market value (CNY).
- **`cancel_order(order)`**, **`get_orders()`**, **`get_open_orders()`**, **`get_trades()`**
- **`record(**numeric_metrics)`** — emit finite numeric metrics per bar/day.
- **`log.info(...)` / `log.warning(...)` / `log.error(...)` / `log.debug(...)`** — write strategy logs (available via `get_run` on failure and `get_run_details(section="strategy_logs")`).

## 4. `context` & `g` Objects

- `g`: persistent user state namespace across callbacks.
- `context.current_dt`: current simulation `datetime`.
- `context.previous_date`: previous trading `date`.
- `context.securities`: tuple of subscribed securities (passed to `run_backtest(securities=[...])` or set via `set_universe`).
- `context.parameters`: dictionary passed via `run_backtest(parameters={...})`.
- `context.portfolio`: `total_value`, `available_cash`, `positions_value`, `starting_cash`, `returns`, `positions` (dict of `security -> Position` with `total_amount`, `closeable_amount`, `value`, `avg_cost`, `price`).

## 5. Unsupported JoinQuant Functions (Do Not Use)

`get_fundamentals`, `query`, `valuation`, `get_index_stocks`, `get_industry_stocks`, `get_security_info`, `get_trade_days`, `get_extras`, `get_factor_values`, `run_weekly`, `run_monthly`, `read_file`, `write_file`, `log.set_level`.
"""

MODEL_NOTES_MD = """# A宽 Backtest & Data Model Notes

1. **Security Codes**: Canonical format is `000001.XSHE` (Shenzhen), `600000.XSHG` (Shanghai), `BJ.830799` (Beijing). MCP tools automatically normalize `.SZ`/`.SH`, but inside Python strategy code always use `.XSHE` / `.XSHG`.
2. **Frequencies & Matching**:
   - `frequency="1d"` (Daily): `run_daily(..., time="open")` submits orders matched at the day's open price; `attribute_history(..., "1d", ...)` at open reads completed sessions up to `context.previous_date`.
   - `frequency="1m"` (Minute): intraday callbacks (`time="09:50"` or `"every_bar"`) match market orders at `next_open` subject to per-minute volume participation limits.
3. **Price Adjustment (`fq`)**:
   - `fq="pre"` (default forward adjustment) and `fq="post"` (backward adjustment) work out of the box from `MarketRelease` daily `preclose` ratios (or use a pinned `ASHARE_QUANT_PYTHON_HISTORY_FACTORS` snapshot when configured); pass `fq=None` for raw unadjusted prices.
4. **A-Share Trading Rules**:
   - T+1 settlement (shares bought today become `closeable_amount` on the next trading day).
   - 100-share board lots; cash dividends and splits are settled on the native account ledger.
5. **Data Provenance**:
   - Missing or suspended observations are never fabricated or forward-filled with invented prices.
"""

TEMPLATES = {
    "daily_rebalance": '''from aquant import *


def initialize(context):
    set_benchmark("000300.XSHG")
    set_universe(context.securities)
    g.window = int(context.parameters.get("window", 5))
    run_daily(rebalance, time="open")


def rebalance(context):
    if not context.securities:
        return
    sec = context.securities[0]
    hist = attribute_history(sec, g.window, "1d", ["close"], fq=None)
    if len(hist) < g.window:
        return
    ma = float(hist["close"].mean())
    last = float(hist["close"].iloc[-1])
    target_shares = int(context.parameters.get("shares", 100)) if last >= ma else 0
    order_target(sec, target_shares)
    record(last_close=last, ma=ma)
    log.info(f"date={context.current_dt.date()} last={last:.2f} ma={ma:.2f} target={target_shares}")
''',
    "minute_timing": '''from aquant import *


def initialize(context):
    set_universe(context.securities)
    g.trade_time = str(context.parameters.get("trade_time", "09:50"))
    run_daily(trade, time=g.trade_time)


def trade(context):
    sec = context.securities[0]
    shares = int(context.parameters.get("shares", 100))
    pos = context.portfolio.positions[sec].total_amount
    target = shares if pos == 0 else 0
    order_target(sec, target)
    log.info(f"minute trade at {context.current_dt.isoformat()} sec={sec} target={target}")
''',
    "multi_file_project": '''# Entrypoint: main.py (referencing sibling module signals.py via relative import)
from aquant import *
from .signals import target_shares


def initialize(context):
    set_universe(context.securities)
    run_daily(rebalance, time="open")


def rebalance(context):
    for sec in context.securities:
        order_target(sec, target_shares(context, sec))
''',
}


def analyze_strategy_code(
    files: dict[str, str], entrypoint: str = "strategy.py"
) -> dict[str, Any]:
    """Run fast static AST checks on single-file or multi-file strategy sources."""
    issues: list[dict[str, Any]] = []
    parsed_trees: dict[str, ast.Module] = {}

    if entrypoint not in files:
        issues.append(
            dict(
                severity="error",
                code="ENTRYPOINT_MISSING",
                file=entrypoint,
                line=1,
                message=f"Entrypoint '{entrypoint}' is not present in submitted files",
                fix=f"Include '{entrypoint}' in project.files or pass single-file source.",
            )
        )

    for filename, content in files.items():
        if not isinstance(content, str) or not content.strip():
            issues.append(
                dict(
                    severity="error",
                    code="EMPTY_SOURCE",
                    file=filename,
                    line=1,
                    message=f"File '{filename}' is empty",
                    fix="Provide valid Python strategy code.",
                )
            )
            continue
        try:
            tree = ast.parse(content, filename=filename)
            parsed_trees[filename] = tree
        except SyntaxError as exc:
            issues.append(
                dict(
                    severity="error",
                    code="PYTHON_SYNTAX_ERROR",
                    file=filename,
                    line=exc.lineno or 1,
                    column=exc.offset or 1,
                    message=f"{filename}:{exc.lineno}: {exc.msg}",
                    fix="Fix the Python syntax error at the reported line.",
                )
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "jqdata" or alias.name.startswith("jqdata."):
                        issues.append(
                            dict(
                                severity="error",
                                code="AQUANT_LEGACY_IMPORT",
                                file=filename,
                                line=node.lineno,
                                message=f"Legacy import 'import {alias.name}' is not supported",
                                fix="Replace 'import jqdata' with 'from aquant import *' or 'import aquant as jqdata'.",
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "jqdata" or mod.startswith("jqdata."):
                    issues.append(
                        dict(
                            severity="error",
                            code="AQUANT_LEGACY_IMPORT",
                            file=filename,
                            line=node.lineno,
                            message=f"Legacy import 'from {mod} import ...' is not supported",
                            fix="Replace 'from jqdata import ...' with 'from aquant import ...'.",
                        )
                    )
                elif mod == "aquant":
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if alias.name in AQUANT_UNSUPPORTED_HINTS:
                            issues.append(
                                dict(
                                    severity="error",
                                    code="AQUANT_UNSUPPORTED_SYMBOL",
                                    file=filename,
                                    line=node.lineno,
                                    message=f"'{alias.name}' is not supported in aquant",
                                    fix=AQUANT_UNSUPPORTED_HINTS[alias.name],
                                )
                            )
                        elif alias.name not in AQUANT_SUPPORTED:
                            issues.append(
                                dict(
                                    severity="error",
                                    code="AQUANT_UNKNOWN_SYMBOL",
                                    file=filename,
                                    line=node.lineno,
                                    message=f"'{alias.name}' is not exported by aquant",
                                    fix=f"Supported symbols: {', '.join(sorted(AQUANT_SUPPORTED))}.",
                                )
                            )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in AQUANT_UNSUPPORTED_HINTS:
                    issues.append(
                        dict(
                            severity="error",
                            code="AQUANT_UNSUPPORTED_CALL",
                            file=filename,
                            line=node.lineno,
                            message=f"Call to unsupported function '{func.id}(...)'",
                            fix=AQUANT_UNSUPPORTED_HINTS[func.id],
                        )
                    )
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "log"
                    and func.attr == "set_level"
                ):
                    issues.append(
                        dict(
                            severity="error",
                            code="AQUANT_UNSUPPORTED_CALL",
                            file=filename,
                            line=node.lineno,
                            message="log.set_level(...) is not supported in aquant",
                            fix="Remove log.set_level(...); log.info/warning/error/debug are enabled by default.",
                        )
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.strip()
                if LEGACY_CODE_LITERAL_RE.fullmatch(val):
                    canonical = normalize_security(val)
                    issues.append(
                        dict(
                            severity="warning",
                            code="NON_CANONICAL_SECURITY_LITERAL",
                            file=filename,
                            line=node.lineno,
                            message=f"String literal '{val}' uses non-canonical exchange suffix/prefix",
                            fix=f"Use canonical code '{canonical}' inside strategy code.",
                        )
                    )

    entry_tree = parsed_trees.get(entrypoint)
    if entry_tree is not None:
        top_funcs = {
            node.name: node
            for node in entry_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "initialize" not in top_funcs:
            issues.append(
                dict(
                    severity="error",
                    code="AQUANT_INITIALIZE_REQUIRED",
                    file=entrypoint,
                    line=1,
                    message=f"Entrypoint '{entrypoint}' must define 'def initialize(context):'",
                    fix="Add 'def initialize(context):' to configure universe/benchmark and schedule callbacks.",
                )
            )
        for cb_name in (
            "initialize",
            "before_trading_start",
            "handle_data",
            "after_trading_end",
            "on_strategy_end",
        ):
            fn = top_funcs.get(cb_name)
            if fn is None:
                continue
            if isinstance(fn, ast.AsyncFunctionDef):
                issues.append(
                    dict(
                        severity="error",
                        code="AQUANT_ASYNC_CALLBACK_UNSUPPORTED",
                        file=entrypoint,
                        line=fn.lineno,
                        message=f"Callback '{cb_name}' cannot be an async function",
                        fix=f"Use synchronous 'def {cb_name}(...):'.",
                    )
                )
            elif any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)):
                issues.append(
                    dict(
                        severity="error",
                        code="AQUANT_GENERATOR_CALLBACK_UNSUPPORTED",
                        file=entrypoint,
                        line=fn.lineno,
                        message=f"Callback '{cb_name}' cannot be a generator",
                        fix=f"Remove yield/yield from inside '{cb_name}'.",
                    )
                )

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    return dict(
        valid=len(errors) == 0,
        error_count=len(errors),
        warning_count=len(warnings),
        issues=issues,
    )


def register_knowledge(server):
    """Register MCP Resources and Prompts on the server instance."""

    @server.resource(
        "aquant://api-reference",
        name="aquant_api_reference",
        title="aquant Strategy API Reference",
        description="Complete function signatures, context/portfolio fields, and unsupported JQ functions for aquant strategies.",
        mime_type="text/markdown",
    )
    def resource_api_reference() -> str:
        return API_REFERENCE_MD

    @server.resource(
        "aquant://model-notes",
        name="aquant_model_notes",
        title="A宽 Backtest & Data Model Notes",
        description="Matching rules (1d vs 1m), price adjustment (fq), A-share T+1/lot rules, and data provenance notes.",
        mime_type="text/markdown",
    )
    def resource_model_notes() -> str:
        return MODEL_NOTES_MD

    @server.resource(
        "aquant://templates/{name}",
        name="aquant_strategy_template",
        title="aquant Strategy Template",
        description="Ready-to-run strategy templates: daily_rebalance, minute_timing, multi_file_project.",
        mime_type="text/x-python",
    )
    def resource_template(name: str) -> str:
        if name not in TEMPLATES:
            return f"# Unknown template '{name}'. Available: {', '.join(sorted(TEMPLATES))}"
        return TEMPLATES[name]

    for key, code in TEMPLATES.items():

        def _make_reader(text: str):
            def _read() -> str:
                return text

            return _read

        server.resource(
            f"aquant://templates/{key}",
            name=f"template_{key}",
            title=f"Template: {key}",
            description=f"Ready-to-run aquant template ({key})",
            mime_type="text/x-python",
        )(_make_reader(code))

    @server.prompt(
        name="research_loop",
        title="Quantitative Research Loop",
        description="Standard workflow: inspect environment & securities -> preflight with check_strategy -> run_backtest -> compare_runs.",
    )
    def prompt_research_loop(goal: str = "Evaluate a quantitative trading hypothesis") -> str:
        return (
            f"Goal: {goal}\n\n"
            "Follow this streamlined v2 workflow:\n"
            "1. Call `get_environment()` and `search_securities(query=...)` to confirm available dates and security codes.\n"
            "2. Read resource `aquant://api-reference` if writing a new strategy. Validate code with `check_strategy(source=..., start_date=..., end_date=...)`.\n"
            "3. Execute with `run_backtest(source=..., start_date=..., end_date=..., frequency='1d' or '1m', wait_seconds=30)` — it waits and returns 20 `returns-v2` metrics directly.\n"
            "4. If iterating on parameters, pass `parameters={...}` to `run_backtest` and call `compare_runs(run_ids=[...])` to compare Sharpe, drawdown, and excess return."
        )

    @server.prompt(
        name="port_joinquant_strategy",
        title="Port JoinQuant Strategy to aquant",
        description="Convert legacy jqdata strategy code to aquant and verify via check_strategy.",
    )
    def prompt_port_joinquant(code: str = "") -> str:
        return (
            "Convert the JoinQuant strategy below to A宽 `aquant`:\n"
            "1. Replace `from jqdata import *` / `import jqdata` with `from aquant import *`.\n"
            "2. Replace unsupported calls (`get_fundamentals`, `get_index_stocks`, `get_security_info`, `run_weekly`, `run_monthly`, `log.set_level`) per `aquant://api-reference`.\n"
            "3. Ensure security codes use `.XSHE` / `.XSHG` and pass `fq=None` in `attribute_history` when historic adjustment factors are not configured.\n"
            "4. Call `check_strategy(source=...)` to verify zero errors before calling `run_backtest`.\n\n"
            f"```python\n{code}\n```"
        )
