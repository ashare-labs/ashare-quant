# 结果分析与收益归因

`ashare_quant.analysis` 基于已验证的冻结回测结果（`PythonResult`）计算只读派生分析（输出 schema `aq.analysis.v1`），底层使用锁定的 `empyrical-reloaded==0.7.0`。

## 调用入口与版本化比较

### Python 函数入口

```python
from ashare_quant.analysis import AnalysisPolicy, analyze_result

analysis = analyze_result(
    frozen_result,
    source_sha256=verified_result_sha256,
    policy=AnalysisPolicy(
        annualization=252,
        risk_free_annual=0.0,
        risk_free_source="assumed_zero",
        min_risk_observations=20,
    ),
    benchmark=frozen_benchmark_or_none,
    calculation_version="returns-v2",
)
```

### HTTP 接口与基准切换

规范路由为 `/api/v1/python/jobs/{id}/result/analysis` 与 `/api/v1/history/jobs/{id}/result/analysis`（兼容短路径 `/jobs/{id}/analysis`）：

```http
GET /api/v1/python/jobs/{id}/result/analysis?analysis_version=returns-v2&benchmark=frozen
```

- **计算版本（`analysis_version`）**：
  - `returns-v2`（前端默认）：主超额收益采用**相对净值口径** `(1 + 策略累计收益) / (1 + 基准累计收益) - 1`；同时在 `return_difference` 中保留算术收益差（百分点），在 `series.relative_nav` 与 `excess_drawdowns` 中提供相对净值序列与超额回撤区间。
  - `returns-v1`（省略参数时的向后兼容默认）：主超额收益采用算术收益差 `策略累计收益 - 基准累计收益`。
- **比较基准选择（`benchmark`）**：
  - `frozen`：使用任务执行时冻结的原始基准（`native.benchmark`）；
  - `none`：显式关闭基准比较；
  - `<指数代码>`（如 `000300.XSHG`，仅限 `returns-v2`）：从该任务已冻结的 `market_release` / `benchmark_release` 中派生比较分析，生成独立的 `analysis_id`，不修改原冻结结果。

---

## 20 项核心指标定义

令 $r$ 为策略日收益序列，$b$ 为同日基准收益序列，$A=252$ 为年化因子，$n$ 为有效交易日数，日无风险利率 $rf_d = (1 + rf_{\text{annual}})^{1/A} - 1$：

| 指标键名 | 计算定义 |
| --- | --- |
| `net_return` | $\text{期末总权益} / \text{期初初始资金} - 1$ |
| `annual_return` | $(1 + \text{net\_return})^{A / n} - 1$ |
| `benchmark_return` | 基准期末收盘点位 / 基期（首交易日前一交易日）收盘点位 $- 1$ |
| `excess_return` | `returns-v2` 为 $(1 + \text{net\_return}) / (1 + \text{benchmark\_return}) - 1$；`returns-v1` 为算术差 |
| `alpha` | 超额日收益线性回归日截距 $\alpha_d$，按 $(1 + \alpha_d)^A - 1$ 年化 |
| `beta` | $\text{Cov}(r, b) / \text{Var}(b)$ |
| `sharpe` | $\text{Mean}(r - rf_d) / \text{Std}(r - rf_d, \text{ddof}=1) \times \sqrt{A}$ |
| `sortino` | $\text{Mean}(r - rf_d) / \sqrt{\text{Mean}(\min(r - rf_d, 0)^2)} \times \sqrt{A}$ |
| `max_drawdown` | 包含初始资金锚点的账户总权益峰谷最大跌幅 |
| `daily_excess_return` | $\text{Mean}(r - b)$ |
| `excess_max_drawdown` | 相对净值序列 $\text{NAV}_p / \text{NAV}_b$ 的峰谷最大跌幅 |
| `information_ratio` / `excess_sharpe` | $\text{Mean}(r - b) / \text{Std}(r - b, \text{ddof}=1) \times \sqrt{A}$（两项同义） |
| `volatility` / `benchmark_volatility` | 策略 / 基准日收益样本标准差 $\times \sqrt{A}$ |
| `daily_win_rate` | 正收益交易日数 / 全部有效交易日数（平盘日计入分母） |
| `win_rate` | 净盈亏 $>0$ 的已闭合持仓周期数 / 全部已闭合周期数 |
| `profit_loss_ratio` | 盈利闭合周期平均净盈亏 / 亏损闭合周期平均净盈亏绝对值 |
| `winning_trades` / `losing_trades` | 已闭合持仓周期中净盈亏 $>0$ / $<0$ 的次数 |

- 年化、波动率、Sharpe、Sortino、Alpha/Beta、IR 要求有效交易日数不少于 `min_risk_observations`（默认 20）；样本不足或缺失基准时对应指标 `status="unavailable"`、`value=null` 并附具体 `reason`。

---

## 输出结构与交易盈亏归因

响应对象包含 `metrics`、`series`（逐日权益、现金、持仓市值、净值、累计收益、回撤、买卖成交额）、`drawdowns`（按深度排序的回撤与恢复区间）、`monthly`（月度复利收益）、`rolling`（20/60/120 日滚动指标）、`costs`（佣金、印花税与已付分红汇总）及 `trading`（交易归因）：

- **闭合持仓周期（Closed Cycles）**：同一证券从空仓买入至再次全部平仓为一个闭合周期（部分减仓不拆分周期，期末未平仓归入 `open_cycles`）。
- **成本与分红归属**：买入手续费计入移动平均持仓成本，卖出税费从卖出成交所得中扣除；登记日持仓对应的已结算现金分红（`gross_cash_no_tax`）归入该次持仓周期的净盈亏（`net_pnl`）。
- **账本对账**：逐日成交现金变动与 `available_cash + locked_cash` 对账，逐日持仓数量与日终快照对账；若输入账本存在时序或金额矛盾，返回 `409 RESULT_ANALYSIS_UNAVAILABLE`。
