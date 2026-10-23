"""Presentation metadata; numerical definitions remain owned by ashare-data."""

import pandas as pd
from ashare_data.factors.alpha191 import EXPLICIT_VERSION, VERSIONS
from ashare_data.factors.catalog import catalog as owner_catalog
from ashare_data.factors.joinquant.panel import MINIMUM
from ashare_data.factors.joinquant.research_view import LOOKBACK

from ashare_quant.contracts import DomainError

# Explanatory notation only. Never evaluated as formulas or used to compute values.
DESCRIPTIONS = {
    "ROC120": (
        "120日价格变动率",
        "动量",
        "100 × (adjusted_close[t] / adjusted_close[t−120] − 1)",
        ["OHLCV", "amount", "adjustment"],
    ),
    "Variance20": (
        "20日收益方差",
        "风险",
        "250 × sample_variance(20 daily returns)",
        ["OHLCV", "amount", "adjustment"],
    ),
    "ATR6": (
        "6日平均真实波幅",
        "技术指标",
        "mean₆(max(high−low, |high−previous_close|, |low−previous_close|))",
        ["OHLCV", "amount", "adjustment"],
    ),
    "ATR14": (
        "14日平均真实波幅",
        "技术指标",
        "mean₁₄(max(high−low, |high−previous_close|, |low−previous_close|))",
        ["OHLCV", "amount", "adjustment"],
    ),
    "EMA5": (
        "5日指数均线比值",
        "技术指标",
        "EMA₅(adjusted_close, 60-session seed window) / adjusted_close[t]",
        ["OHLCV", "amount", "adjustment"],
    ),
    "VROC6": (
        "6日量变动率",
        "量价",
        "100 × (research_adjusted_volume[t] / research_adjusted_volume[t−5] − 1)",
        ["OHLCV", "amount", "adjustment"],
    ),
    "TVMA6": ("6日成交金额均值", "量价", "mean₆(raw_amount)", ["OHLCV", "amount"]),
    "VOL5": (
        "5日平均换手率",
        "量价",
        "mean₅(100 × raw_volume / circulating_A_shares)",
        ["OHLCV", "amount", "shares"],
    ),
    "circulating_market_cap": (
        "流通市值",
        "规模",
        "raw_close × circulating_A_shares",
        ["OHLCV", "amount", "shares"],
    ),
    "net_profit_to_total_operate_revenue_ttm": (
        "净利润与营业总收入之比",
        "质量",
        "net_profit_TTM / total_operating_revenue_TTM",
        ["financials"],
    ),
    "net_profit_growth_rate": (
        "净利润增长率",
        "成长",
        "(net_profit_TTM − prior_net_profit_TTM) / prior_net_profit_TTM",
        ["financials"],
    ),
    "operating_revenue_ttm": (
        "营业收入TTM",
        "财务",
        "sum(four visible quarters of operating_revenue)",
        ["financials"],
    ),
    "net_operate_cashflow_growth_rate": (
        "经营现金流增长率",
        "成长",
        "(operating_cashflow_TTM − prior_TTM) / prior_TTM",
        ["financials"],
    ),
    "BIAS5": (
        "5日乖离率",
        "技术指标",
        "100 × (adjusted_close / mean₅(adjusted_close) − 1)",
        ["OHLCV", "amount", "adjustment"],
    ),
    "CCI20": (
        "20日顺势指标",
        "技术指标",
        "(TP − mean₂₀(TP)) / (0.015 × mean₂₀(|TP − mean₂₀(TP)|)); TP=(H+L+C)/3",
        ["OHLCV", "amount", "adjustment"],
    ),
    "PSY": (
        "心理线指标",
        "技术指标",
        "100 × count(close[t] > close[t−1], 12) / 12",
        ["OHLCV", "amount", "adjustment"],
    ),
    "quick_ratio": (
        "速动比率",
        "质量",
        "(current_assets − inventory) / current_liabilities",
        ["financials"],
    ),
    "debt_to_asset_ratio": (
        "资产负债率",
        "质量",
        "total_liabilities / total_assets",
        ["financials"],
    ),
    "net_operating_cash_flow_coverage": (
        "净利润现金含量",
        "质量",
        "quarter_operating_cashflow / quarter_parent_net_profit",
        ["financials"],
    ),
}


def catalogue(version=EXPLICIT_VERSION):
    if version not in VERSIONS:
        raise DomainError("FACTOR_VERSION", "Choose an explicit supported definition version", 422)
    result = []
    for (library, name), row in owner_catalog(alpha191_version=version).iterrows():
        alpha = library == "alpha191"
        qlib = library == "qlib_alpha158"
        title, category, expression, inputs = (
            (
                name.upper(),
                "Qlib Alpha158" if qlib else "Alpha191",
                row["expression"],
                list(row["dependencies"]),
            )
            if alpha or qlib
            else DESCRIPTIONS[name]
        )
        warmup = row["full_window_rows"] if alpha or qlib else MINIMUM.get(name, LOOKBACK.get(name))
        parameters = []
        if alpha and name == "alpha143":
            parameters.append(
                dict(name="alpha143_seed", label="递推初值", kind="number", required=True)
            )
        if alpha and name == "alpha146" and version == EXPLICIT_VERSION:
            parameters.append(
                dict(
                    name="alpha146_m",
                    label="SMA 平滑参数 m",
                    kind="integer",
                    required=True,
                    minimum=1,
                    maximum=60,
                )
            )
        result.append(
            dict(
                id=f"{library}:{name}",
                library=library,
                field=name,
                name=title,
                category=category,
                definition=row["definition"],
                expression=expression,
                description=(
                    "Qlib v0.9.7 默认价量特征；部分窗口、float32、缺失和非有限值遵循官方规则。"
                    if qlib
                    else "GTJA Alpha191 显式价量研究公式。排名在本次固定股票范围内计算。"
                    if alpha
                    else "聚宽命名字段的已有研究定义；数值精度和来源口径沿用 AD。"
                ),
                inputs=inputs,
                warmup_policy=row.get("warmup_policy") if qlib else None,
                source=row.get("source") if qlib else None,
                license=row.get("license") if qlib else None,
                warmup_rows=int(warmup) if pd.notna(warmup) else None,
                recursive_history=bool(row["recursive_or_event_history"])
                if alpha
                else name == "EMA5",
                cross_section_rank=bool(row["cross_section_rank"]) if alpha else False,
                availability=row["availability"],
                note=row["note"],
                parameters=parameters,
                entrypoint=row["entrypoint"],
                definition_versions=list(VERSIONS) if alpha else [row["definition"]],
                limitations=(
                    [
                        "158 是特征列数，不是有效策略数。",
                        "窗口长度是上下文需求；官方允许部分窗口输出。",
                        "VWAP 与成交量口径以冻结输入为准；不宣称供应商逐值等价。",
                    ]
                    if qlib
                    else []
                )
                + [
                    "固定来源快照不等于历史时点认证或供应商逐值等价。",
                    "缺失值和预热不足保留原因，不补零。",
                ],
            )
        )
    return result


def detail(factor_id, definition):
    version = definition if factor_id.startswith("alpha191:") else EXPLICIT_VERSION
    item = next((r for r in catalogue(version) if r["id"] == factor_id), None)
    if item is None or item["definition"] != definition:
        raise DomainError("FACTOR_NOT_FOUND", "Unknown factor or definition version", 422)
    return item
