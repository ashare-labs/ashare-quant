# Python 分钟策略首批支持

`python -m ashare_quant.minute` 在独立进程执行可信 Python 脚本，复用 RQAlpha
`SimulationBroker`、`DefaultBarMatcher`、`Account`。支持固定主板多股票、共享资金、
跨日持仓及分钟事件。没有新增撮合账本，不接入任务服务或修改旧任务恢复格式。

这是明确约定行为的 **JQ 风格 API 子集**。真实 Python 代码已经执行，不代表真实行情、
聚宽服务端对照或全 API 兼容。结果分别记录 `real_strategy_executed`、
`real_market_data` 和 `jq_comparison`；当前对照状态始终 `NOT_PERFORMED`。

## 可运行示例

按主 README 安装固定原生依赖，然后执行：

```sh
mkdir -p .verification/minute-demo
.venv/bin/python examples/python_minute/make_fixture.py .verification/minute-demo/input.json
.venv/bin/python -m ashare_quant.minute --input .verification/minute-demo/input.json --preflight
.venv/bin/python -m ashare_quant.minute \
  --input .verification/minute-demo/input.json \
  --strategy examples/python_minute/equal_weight.py \
  --output .verification/minute-demo/equal-weight
```

示例输入是三股、三个交易日、每日 240 个分钟的 **2160 行人工数据**。
另有 `rotation.py`（分钟动量选股）和 `limit_and_exit.py`（午休挂单、分笔成交、次日卖出）。
每个策略使用独立输出目录；脚本包含普通 Python 状态、循环、条件和 pandas 运算，
并非预先录制的事件回放。

Python 调用入口为 `ashare_quant.minute.run_backtest(strategy, input_path, output, ...)`。
配置允许 `initial_cash`、`volume_percent`、`commission_rate`、`sell_tax`、`min_commission`。
默认值依次为 100000、0.25、0.0003、0.001、5。滑点固定零。
费用是显式研究模型，不宣称任意历史日期的法定费率。调整配置须新建运行目录。

## 生命周期与时间

1. 载入脚本并调用一次 `initialize(context)`；`g` 和 context 自定义属性在本次运行保留。
2. 每日 09:00 调用 `before_trading_start(context)`，再按注册顺序运行该时点 `run_daily` 回调。
3. 声明时段起点只运行该时刻的定时回调；每个分钟结束点先由原生 broker 处理先前订单，
   再按注册顺序运行定时/`every_bar` 回调，最后调用 `handle_data(context, data)`。
4. 15:30 原生 broker 终结日单，然后执行该时点定时回调及 `after_trading_end(context)`。
   原生结算决定 T+1 可卖数量。

策略 `context.current_dt` 是上海本地无时区 datetime；持久证据时间有 `+08:00`。
没有午休分钟，也不填缺失 K 线。`11:30` 回调可以留单到 `13:01`，`13:00` 时钟没有撮合条。
最后收盘分钟可读数据，不能提交新的订单；盘前/盘后下单显式拒绝。
`run_daily` 支持 `before_open`、`open`、`after_close`、`every_bar`，及全部声明日共有的精确时刻。
午休时刻、周/月调度、动态注册不在范围内。

数据按 AD 提供的 `bar_end <= current_dt` 且 `available_at <= current_dt` 可见。
`history`、`attribute_history`、`get_price` 包含恰好结束且已可见的分钟；
`get_bars(include_now=False)` 排除恰好当前结束的分钟。未来端点请求报错，历史不足也报错。
`get_current_data().last_price` 取当天最新可见闭合价，开盘前取 AD 明示的昨收；
`day_open` 须等待第一根可见分钟。此约定尚未证实等同聚宽的时间标签。

订单只能使用提交时刻之后的输入区间：撮合区间 `bar_start >= submitted_at`，且 `bar_end > submitted_at`。
成交价格取该区间开盘价；成交量上限由原生 matcher 按整段量及参与率计算。
这是事后分钟容量研究模型，无逐笔队列或真实瞬时成交承诺。
限价单保留未成交余额，午休不断单，日末以原生 `REJECTED` 状态失效；
市价单只参加首个可撮合分钟，部分成交后原生取消余额。零量分钟的限价单继续等待。
同股多单共享该分钟容量，不同股票共享原生账户现金。

## 支持与迁移边界

| 接口 | 首批范围 |
|---|---|
| `initialize`、`before_trading_start`、`handle_data`、`after_trading_end` | 真实 Python 生命周期；支持 `from jqdata import *` 或省略导入 |
| `context`、`g` | `current_dt`、`previous_date`、`run_params`、自定义状态 |
| `context.portfolio` | `available_cash`、`locked_cash`、`total_value`、`positions_value`、`starting_cash`、`positions/long_positions` |
| position | `security`、`total_amount`、`closeable_amount`、`avg_cost`、`price`、`value`，原生账户只读投影 |
| `run_daily`、`set_universe` | 固定证券集合及上述时刻；reference_security 必须在输入集合内 |
| `history` | `1m/minute`，单字段、多股时间对齐 DataFrame 或字典；`fq=None` |
| `attribute_history` | `1m/minute`，单股多字段，DataFrame 或字典；`fq=None` |
| `get_bars` | `1m/minute`，明确 `include_now`，结构数组或 DataFrame；不支持复权参考日 |
| `get_price` | `1m/minute`、`count` 窗口、单股时间索引/多股 time-code 长表；`fq=None,panel=False,fill_paused=False` |
| `get_current_data` | 固定证券的 `last_price`、`paused`、`high_limit`、`low_limit`、有证据才提供的 `is_st`、可见后 `day_open` |
| `order`、`order_target`、`order_value`、`order_target_value` | 原生数量/金额 API；单股票账户、long、T+1；市价及 `LimitOrderStyle` |
| `get_orders`、`get_open_orders`、`cancel_order`、`get_trades` | 原生订单/成交投影；失败下单可返回 None，拒绝原因保留事件日志 |
| `record`、`log` | 有限数值记录和文本日志；日志级别变更不支持 |
| `set_option` | 仅 `use_real_price=True`、`avoid_future_data=True` |
| `set_order_cost`、`set_slippage` | 校验脚本声明与冻结配置相等；不得静默忽略不同费率或非零滑点 |

默认 JQ `fq='pre'` 不会偷偷转原价，迁移脚本需要明确 `fq=None`。
历史数据仅含给定分钟窗口；首分钟前需要历史的策略必须由 AD 另提供获准窗口，当前不自动下载。
整日停牌不合成历史条；多股历史索引不齐则拒绝。`skip_paused=False` 不承诺聚宽的停牌填价。
目标订单按原生当前持仓计算，不自动扣除旧挂单；策略应先查询/撤单，避免重复目标请求。
金额下单、整手取整、最小佣金、市价部分成交及现金冻结遵从原生模型，不宣称聚宽完全一致。
保留原生浮点结果，极小浮点冻结尾差可能存在，不把它改写成另一份账本。

明确不支持：日频/5m 历史查询、前后复权、基本面/PIT/因子、历史全市场股票池/指数成分、
基准收益、公司行动分钟入账、期货/基金/科创创业板/北交所、多个子账户、short、tick、
集合竞价、GTC/IOC 自定义有效期、周/月调度、任意文件 API、实盘、恶意代码沙箱。
已知 API 名称有显式 `JQ_API_UNSUPPORTED`；未知 jqdata 导入同样失败。未知 Python 全局保留 Python 错误。

## 最小 AD 输入

`ad-minute-backtest.v1` 是 AQ 消费契约，不是新的源采集服务。
示例生成器可查看完整字段形状。真实数据要求 `producer.name=ashare-data`，
并保留版本/提交/来源摘要、`source_refs`、`limitations` 和 `real_market_data`。

- `securities`：1..20 个显式沪深主板证券；`days` 最多60个连续声明交易日。
- `calendar`：包含前一和后一实际交易日；`sessions` 明示各日连续区间，不能穿过午休。
- `states`：每证券日的 listed、suspended、上下限、昨收、可选 ST，及盘前可见证据。
  未知值不能转正常交易、零或自动推导的10%涨跌停。
- `minute`：每个非停牌声明分钟都有原价 OHLC、整数股 volume、元 amount、
  `bar_start/bar_end`、`available_at`、逐条 evidence。重复、缺行、无效 OHLC、提前可见均拒绝。
- `actions=[]` 且 `action_coverage.status=source_covered_no_actions` 才在真实首批范围；
  人工样本用 `synthetic_known_no_actions`。有公司行动直接报未支持，交由 AD 及原公司行动线推进。
- `visibility=declared_available_at` 保留导入者声明，**不认证供应商历史 PIT**。
  `assumed_closed` 须调用者显式 `--accept-assumed-visibility`，仍需 AD 给出区间，不能在 AQ 猜测源标签。

`export_ad_snapshot(snapshot, metadata, path)` 使用既有 AD 公共 `Snapshot.bars` 查询并投影区间、
可见时刻、单位与行摘要；不读取 AD 内部 DB、不采集、不填价。本批已用真实 AD Store 的
import→validate→publish→snapshot→export 链验证人工分钟数据。
AQ 固定运行时仍为已锁定的 AD 0.8.0.dev2；本接口不擅自更新服务依赖。

真实公开源的源标签不等于已证闭合区间，未知历史状态/公司行动不能由 AQ 补成执行事实。
缺这些证据时完整真实分钟回测仍阻断，应由 AD 交付带限定的资产，再单独验证经济行为。

## 运行记录、失败与恢复

每次独立进程冻结脚本、输入、运行时/后端源码身份、原生配置；保存逐回调事件、订单、成交、
账户和结果摘要。完整结果再次打开会验证全部产物 hash 后直接返回，策略不会再执行。
任一文件变化报错；失败/超时保留日志且无 `complete.json`，须新目录重跑。
本入口不恢复任意 Python 中途状态；原有日频任务和 checkpoint 格式保持原实现。
网络审计钩子阻止策略误用外部取数，独立进程和超时不构成恶意代码安全边界。

回归入口：`pytest tests/minute -q`；全仓测试和固定日频九案例另行验收。
真实行情、JQ 浏览器日志及个人运行证据不进入源码包。

官方参考：聚宽[API 帮助](https://www.joinquant.com/help/api/help)在本次公开读取时返回地区限制；
[官方 SDK](https://github.com/JoinQuant/jqdatasdk/blob/ba61143a6504bb1f797b16b8f9d23a21909c4f60/jqdatasdk/api.py)
仅提供数据函数签名参考。没有使用正在被因子线占用的浏览器或额度。
