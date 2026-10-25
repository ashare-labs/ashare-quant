# 冻结结果的派生分析

`ashare_quant.analysis` 是计算模块，不执行策略，不访问行情，不修改输入、任务或历史结果。输入必须由调用方先完成冻结文件完整性校验。输出是 JSON 可序列化的 `aq.analysis.v1`。纯函数默认保留 `returns-v1`；显式传入 `calculation_version="returns-v2"` 使用相对净值超额收益，输出及分析身份均记录版本。

## 相对净值口径（returns-v2）

主超额收益为 `(1 + 策略累计收益) / (1 + 基准累计收益) - 1`。策略从 100 到 120、基准从 100 到 110 时，超额为 9.0909%，不是收益差 10 个百分点。指标、每日曲线及月度超额使用这一口径；超额回撤继续基于同一相对净值及期初值 1。

v2 保持首页的 20 项指标结构。辅助 `return_difference` 为 `{value, unit: "percentage_points", ...}`，数值 10 表示 10 个百分点；每日及月度同名字段也以百分点存储。`series.relative_nav` 提供相对净值，`excess_drawdowns` 提供相应回撤与恢复区间。辅助收益差不要求进入首页。

Alpha、Beta、信息比率、日均超额收益仍遵循下文各自的回归、协方差、主动日收益及跟踪误差定义，不因主超额口径改变而改成除法。日胜率仍为正收益日占比；盈亏比仍为闭合周期平均盈利/平均亏损；超额夏普目前明确与信息比率同义。这些定义不是对任何第三方平台全部公式等价的声明。

v1 保留原累计收益差语义及输出结构。版本改变生成不同 `analysis_id`，`source_sha256` 仍指向同一原结果；不重写冻结文件，不回填成交或假造平仓。

## 接入与依赖

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
)
```

必须显式提供年化因子、无风险假设及最短风险样本数。上述策略是产品可以选择的明确约定，不是库内隐式默认。252 代表交易观察值的年化约定，不用于生成美国或 A 股日历。分钟执行也从账户点聚合日终评价；不套分钟年化系数。

函数使用 **empyrical-reloaded 0.7.0**，调用时校验版本。接线方须将这一版本及其传递依赖纳入产品依赖锁；此模块提交不修改执行线程拥有的 `pyproject.toml`、`uv.lock` 或任务路由。库许可为 Apache-2.0，官方来源为 [empyrical-reloaded](https://github.com/stefan-jansen/empyrical-reloaded)。不安装数据下载 extras，不调用数据下载工具；原版 empyrical 与 reloaded 共用 import 名，不能并装。

HTTP入口为 `/api/v1/python/jobs/{id}/result/analysis` 与 `/api/v1/history/jobs/{id}/result/analysis`，共用这一个计算函数和原历史结果的完整性校验；短路径 `/jobs/{id}/analysis` 是同一读取操作的兼容别名。输出 `source_sha256` 是核验过的原发布文件摘要。重复GET确定且只读，不覆盖 `published.json`、不触发回测、不重读今天的行情或默认基准。仅支持含账户序列的Python回测；其他历史结果仍按原接口可读。

`GET /api/v1/python/backtests` 的每条成功记录在读取已验证冻结结果后，将同一分析的 `metrics[key].value` 投影至 `items[].summary[key]`；保留期初资金、期末权益和成交数等原汇总字段。缺基准等不可用指标保留 null，真实零收益保留 0。整体分析失败由独立的 `analysis_error` 返回，不把仍完整可读的结果误标为不可用；结果完整性失败仍使用 `result_error`。此列表投影不更改 `/result` 返回的原始冻结数据。对比页应读取 `/analysis` 并校验 `job_id` 和原 `result.snapshot_sha256`，不能期望旧 `/result.summary` 含后来新增的指标。

接线采用上面的明确产品约定。依赖及传递闭包已纳入 `pyproject.toml`、`uv.lock` 和运行时快照。`native.benchmark` 的冻结基期/逐日收盘点位投影为输入，身份取完整冻结基准对象摘要；未提供可核实名称时显示证券代码。缺参考数据保留执行时的具体 `BENCHMARK_*` 原因和 `missing_dates`，独立策略指标继续可用。账本不一致时返回结构化409与 `RESULT_ANALYSIS_UNAVAILABLE`，不返回看似正常的虚构统计。

## 输入

`frozen_result` 沿用现有 PythonResult：

- `job_id`、`summary.initial_cash`；若汇总包含净收益率/最大回撤，会与序列复算比对。
- `native.accounts`：`at`（带时区 ISO，或单独日期）、`total_value`、`available_cash`、`locked_cash`、`positions_value`、`positions`。持仓至少含 `total_amount/value`。同日多个账户点使用最后一个；时间须单调且不得重复。
- `preparation.trading_dates` 若存在，必须与聚合后的账户日期完全相同，禁止静默跳过缺失交易日。缺少日历时可显示权益累计变化，但年化/风险指标保留为空，不宣称账户观察点覆盖全部交易日。
- `native.trades`：`trade_id/order_id/security/at/side/amount/price/commission/tax`。买卖方向为 BUY/SELL。
- `native.corporate_entitlements` 与 `corporate_account_model` 供交易归因；目前只支持已结算、登记日持仓可核对、`gross_cash_no_tax` 的现金分红。送转、未结算分红或不支持的税模型返回归因缺失原因。

基准输入格式：

```text
security: string
name: string
return_type: "price" | "total_return"
source_sha256: 64-character lowercase SHA256
initial_date: YYYY-MM-DD, before the first evaluation session
initial_value: positive number, previous applicable close
points: [{date: YYYY-MM-DD, value: positive number}]
```

基准 points 必须与策略交易日逐项相同，不能从第一个非零收益日开始裁剪，不前填/后填未知价格。无基准或无效基准只使相关指标缺失，独立策略指标仍计算。交易归因不完整也不影响已有账户收益统计。

输入存在账户顺序、覆盖、权益值、汇总一致性等严重问题时，抛出带 `code` 的 `AnalysisInputError`；不得返回一套看似完整的统计。外部出入金尚不支持。归零后重新出现权益会被拒绝；连续零权益保留总亏损/回撤，无法定义的后续日收益为 null。

## 输出

- 身份：`schema_version/calculation_version/analysis_id/job_id/source_sha256/engine`。analysis_id 包含源摘要、基准、策略与计算版本，确定且可重复。
- `policy`：上述参数以及时区、日频评价、现金流、回撤符号和指标定义；`quality` 继承 PIT、公司行为模型、执行频率与缺失原因。
- `metrics`：每项为 `{value, unit, status, reason, sample_count}`。`status` 是 ok 或 unavailable；无值必为 null，不能输出 NaN/Infinity。
- `series`：每个交易日的权益、现金、持仓价值、日收益/盈亏、净值、累计策略/基准/超额收益、回撤、买卖成交额。
- `drawdowns`：所有回撤区间按深度排序，含峰值/谷值/恢复日、下跌/恢复/水下交易日数。起点为初始资金时 peak_date 为 null；未恢复时 recovery_date/recovery_sessions 为 null。
- `monthly`：按日收益复利；`boundary_month` 标识评价区间首末月，不能将其当完整自然月比较。
- `rolling`：20/60/120 个交易日窗口的累计收益、年化波动与 Sharpe；窗口不足不产生记录。
- `costs`：佣金、税、成交成本合计、已支付现金分红及其独立可用状态。费用已经体现在权益，不再从净值收益二次扣除。
- `trading`：归因状态、closed_trades、open_cycles、逐成交 fill_pnl、每日持仓 positions。无法对账时不保留部分正确值冒充全量归因。

收益率字段均为小数，例如 0.1 表示 10%；`unit: percent` 指展示时乘 100。金额为人民币元，股数为数量。指标回撤深度为正，日序列 drawdown 为负。

## 指标定义

令 `r` 为日收益，`b` 为同日基准收益，`A` 为年化因子，`n` 为有效交易日数。首日以前置初始权益计算，合法空仓日保留。`rf_d = (1 + rf_annual)^(1/A)-1`。

| 键 | 定义 |
|---|---|
| net_return | 期末/期初权益 − 1；复核并优先保留冻结汇总的等价值 |
| annual_return | `(1 + net_return)^(A/n)-1` |
| benchmark_return / excess_return | 基准累计收益；策略累计收益减基准累计收益 |
| alpha | 超额日收益回归截距，按 `(1 + daily_alpha)^A-1` 年化 |
| beta | `cov(r,b)/var(b)` |
| sharpe | `mean(r-rf_d)/std(r-rf_d, ddof=1)*sqrt(A)` |
| sortino | `mean(r-rf_d)/sqrt(mean(min(r-rf_d,0)^2))*sqrt(A)` |
| max_drawdown | 包含初始资金的权益峰谷最大跌幅 |
| daily_excess_return | `mean(r-b)` |
| excess_max_drawdown | 相对净值 `NAV_p/NAV_b` 的峰谷最大跌幅 |
| information_ratio | `mean(r-b)/std(r-b, ddof=1)*sqrt(A)` |
| excess_sharpe | v1 明确为 information_ratio 的同义展示，保留原设计条目；不借库的不同公式制造差异 |
| volatility / benchmark_volatility | 对应日收益样本标准差乘 `sqrt(A)` |
| daily_win_rate | 正收益日数/全部有效交易日数，平盘日计入分母 |
| win_rate | 盈利的已闭合持仓周期数/全部已闭合周期数，平手计入分母 |
| profit_loss_ratio | 盈利周期平均净盈亏/亏损周期平均净盈亏绝对值 |
| winning_trades / losing_trades | 已闭合周期中净盈亏大于/小于零的次数 |

年化收益、波动、Sharpe、Sortino、Alpha/Beta、IR 受 `min_risk_observations` 约束；累计收益和回撤可在单个有效日显示。无风险未知不阻断波动/Beta/IR。零方差、无下行收益、零跟踪误差、无已闭合交易分别有原因码。

## 交易和持仓盈亏

同证券由空仓到再次空仓是一个闭合周期；部分卖出不增加周期数，期末未平仓不虚构卖出。买入费用进入移动成本，卖出费用从成交所得扣除。逐卖出成交 PnL 不含另列分红；周期净 PnL 加入对应登记日持仓获得的已支付分红。分红即使在卖出后支付，也归属原周期，不能算到下一笔持仓。

每日成交现金变化与 `available_cash + locked_cash` 对账，持仓数量与日终快照对账。每日未实现盈亏是记录市值减含买入费的移动成本；不再重复添加分红。无法匹配初始持仓、金额、数量、公司行为时返回原因，不能用订单的 avg_cost 当卖出成本。

送转归因和尚未结算的分红在 v1 保守留空；这不改变引擎如何执行/结算它们。市场行情质量、PIT 和复权/分红模型仍以原冻结证据为准，本分析不把它们升级为已验证。

## 验证

`tests/analysis/` 包含可手算的收益、回撤恢复、年化/RF、基准对齐、无交易/短样本/零波动/亏损、分钟聚合、部分平仓、费用与分红、现金/持仓对账、输入不变和离线测试。库输出与独立公式及金额账本对照，不以库输出自证。

`tests/analysis/fixtures.py` 只生成合成的前端契约测试数据；不是策略、行情下载器、生产任务或演练记录。测试不向任何服务提交回测。
