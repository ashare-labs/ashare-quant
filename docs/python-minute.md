# Python 分钟策略首批支持

`python -m ashare_quant.minute` 在独立进程执行可信 Python 脚本，复用 RQAlpha
`SimulationBroker`、`DefaultBarMatcher`、`Account`。支持固定主板多股票、共享资金、
跨日持仓及分钟事件。没有新增撮合账本，不接入任务服务或修改旧任务恢复格式。

这是明确约定行为的 **JQ 风格 API 子集**。真实 Python 代码已经执行，不代表真实行情、
聚宽服务端对照或全 API 兼容。结果分别记录 `real_strategy_executed`、
`real_market_data` 和 `jq_comparison`；单次本地运行继续记录 `NOT_PERFORMED`。
另有 [两例真实聚宽分钟对照](../examples/jq_minute_comparison/README.md)：
2026-10-09/12 两股的信号及委托计划一致。默认研究模式保留原有撮合与资金行为；
`0.11.0.dev15` 的 [显式执行选项](../examples/jq_minute_comparison/execution-policies.md)
以同一真实参考复验，两例成交时刻、数量、价格、费用及最终现金对齐。
比较证据独立保存，不改写旧运行结果；估值边界、容量和对象接口仍有明确差异。

`0.11.0.dev16` 新增显式完整分钟生命周期和每单容量模型，见
[完整生命周期与容量政策](../examples/jq_minute_comparison/session-policies.md)。
默认严格分钟模式不变。新增真实输入依赖 AD 的分角色会话观测；工程测试不代替真实平台对照。

## 可运行示例

需要真实行情的端到端例子，见 [TDX 两股分钟研究样例](../examples/tdx_minute_research/README.md)：
924 条真实原生分钟、两份 Python 策略、共享现金/T+1、完整经济结果和独立重放。
它采用另行命名且显式 opt-in 的研究 profile，保留未知交易所限价、历史时点和行动覆盖限制。
原三股严格题及 B27 仍缺证；以下合成例子和原严格输入契约不变。

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
另外支持 `matching="next_open"|"jq_current"`、`cash_policy="reject"|"resize"`，
默认分别为 `next_open` 和 `reject`。选项、源代码和输入都计入完成身份；
换版本或换选项重用完成目录会失败，旧任务服务/数据库和恢复契约不变。

## 默认生命周期与时间

以下描述 `lifecycle="closed_bars", capacity_policy="strict_minute"` 的默认行为。
显式新模式的回调顺序、午间定时、历史预热及盘后挂单边界见上述新模式文档。

1. 载入脚本并调用一次 `initialize(context)`；`g` 和 context 自定义属性在本次运行保留。
2. 每日 09:00 调用 `before_trading_start(context)`，再按注册顺序运行该时点 `run_daily` 回调。
3. 声明时段起点只运行该时刻的定时回调；每个分钟结束点先由原生 broker 处理先前订单，
   再按注册顺序运行定时/`every_bar` 回调，最后调用 `handle_data(context, data)`。
4. 15:30 原生 broker 终结日单，然后执行该时点定时回调及 `after_trading_end(context)`。
   原生结算决定 T+1 可卖数量。

生命周期与定时回调只支持同步函数。载入、注册和实际调用均校验回调；
`async def`、generator、async generator，以及同步包装函数返回的 awaitable/generator
执行对象，都显式失败，保留 `failure.json`，不写 `complete.json`。
不会因为创建了尚未执行的对象就报告策略成功，也不会自动启动另一个异步运行器。

策略 `context.current_dt` 是上海本地无时区 datetime；持久证据时间有 `+08:00`。
没有午休分钟，也不填缺失 K 线。`11:30` 回调可以留单到 `13:01`，`13:00` 时钟没有撮合条。
最后收盘分钟可读数据，不能提交新的订单；盘前/盘后下单显式拒绝。
`run_daily` 支持 `before_open`、`open`、`after_close`、`every_bar`，及全部声明日共有的精确时刻。
午休时刻、周/月调度、动态注册不在范围内。

数据按 AD 提供的 `bar_end <= current_dt` 且 `available_at <= current_dt` 可见。
TDX 研究 profile 改用独立的 `replay_bar_start/replay_bar_end` 模型字段；源区间与可见时点仍为 null。
`history`、`attribute_history`、`get_price` 包含恰好结束且已可见的分钟；
历史 `end_date/end_dt` 只限制条的结束时间，不替代当前知识时钟。例如 09:31 的条在
09:34 才可见，09:35 查询 `end_date=09:31` 可以取得它；09:33 同样查询仍失败。
`get_bars(include_now=False)` 仅排除恰好 `context.current_dt` 结束的分钟；
显式过去终点仍是包含该终点的窗口。未来端点请求报错，历史不足也报错。
`get_current_data().last_price` 取当天最新可见闭合价，开盘前取 AD 明示的昨收；
`day_open` 须等待第一根可见分钟。两例实测历史窗口和提交当前价相同，
完整生命周期及分钟边界仍不等同聚宽，范围见上述对照。

原生账户在 BAR 更新持仓末价；适配层还在 09:00 盘前、15:30 盘后回调前以及日终/
最终快照前，经原生 `Position.update_last_price` 应用同一时钟已可见价格。
15:00 结束、15:01 可见的条因此可用于 15:30 估值；15:30 后才可见的条仍不使用。
估值更新不会生成额外 BAR、撮合迟到条或新增成交，也不另算一份权益覆盖原生账户。
次日盘前用当日已准入的前收参考更新持仓；它不使仍未可见的历史分钟在查询中提前出现。
最终权益是最后事件时钟（15:30）可见信息下的原生估值，不承诺已收到最后发布版本。

默认 `next_open` 只能使用提交时刻之后的输入区间：`bar_start >= submitted_at`，且 `bar_end > submitted_at`。
成交价格取该区间开盘价。两种撮合模式都复用原生日内容量计数，
将原始十进制 volume × 参与率以精确有理数向下取整到整手，扣除该股同分钟已成交量。
例如 `99500 × 0.001 = 99.5` 股，不能提升为 100 股。该修复默认启用。
这是事后分钟容量研究模型，无逐笔队列或真实瞬时成交承诺。
限价单保留未成交余额，午休不断单，日末以原生 `REJECTED` 状态失效；
市价单只参加首个可撮合分钟，部分成交后原生取消余额。零量分钟的限价单继续等待。
同股多单共享该分钟容量，不同股票共享原生账户现金。

`matching="jq_current"` 在当前确已闭合且可见的分钟同步尝试新单，以该条 close 为价；
新单不使用该条 high/low，也不使用下一条 open。未成交限价单在后续完整区间，
买价严格大于 low、卖价严格小于 high 时按委托价成交；触碰相等不成交。
开盘、13:00、当前条缺失或尚未可见时提交新单显式报错，挂单可跨午休保留。
初始容量仍按分钟共享上限，不实现聚宽文档的每单全日成交量上限，不能宣称完整聚宽撮合。

`cash_policy="resize"` 启用原生数量/目标数量订单的资金不足转金额下单行为，
下单前按可用共享资金、整手及配置费用缩小数量；不足一手返回 None。
金额/目标金额 API 本身就有原生资金限额。`reject` 保留原数量订单拒单行为。
该选项不关闭现金风控，也不保证挂单在未来跳空后继续缩量；未来执行资金不足仍可拒绝/取消。
佣金、每单累计最低费、撤单重下后的新最低费和卖税均继续来自原生账户/费用实现。

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
| `get_price` | `1m/minute`、`count` 窗口、单股时间索引/多股 time-code 长表；`fq=None,panel=False,fill_paused=False`；`jq_minute` 配合 AD 会话观测另支持已可见日 close/volume |
| `get_current_data` | 固定证券的 `last_price`、`paused`、`high_limit`、`low_limit`、有证据才提供的 `is_st`、可见后 `day_open` |
| `order`、`order_target`、`order_value`、`order_target_value` | 原生数量/金额 API；单股票账户、long、T+1；市价及 `LimitOrderStyle` |
| `get_orders`、`get_open_orders`、`cancel_order`、`get_trades` | 原生订单/成交投影；失败下单可返回 None，拒绝原因保留事件日志 |
| `record`、`log` | 有限数值记录和文本日志；日志级别变更不支持 |
| `set_option` | `use_real_price=True`、`avoid_future_data=True`；显式 `jq_daily_initial` 另允许初始化声明与配置完全相等的 `order_volume_ratio` |
| `set_order_cost`、`set_slippage` | 校验脚本声明与冻结配置相等；不得静默忽略不同费率或非零滑点 |

默认 JQ `fq='pre'` 不会偷偷转原价，迁移脚本需要明确 `fq=None`。
历史数据仅含给定分钟窗口；首分钟前需要历史的策略必须由 AD 另提供获准窗口，当前不自动下载。
整日停牌不合成历史条；多股历史索引不齐则拒绝。`skip_paused=False` 不承诺聚宽的停牌填价。
目标订单按原生当前持仓计算，不自动扣除旧挂单；策略应先查询/撤单，避免重复目标请求。
金额下单、整手取整、最小佣金、市价部分成交及现金冻结遵从原生模型，不宣称聚宽完全一致。
保留原生浮点结果，极小浮点冻结尾差可能存在，不把它改写成另一份账本。

明确不支持：上述已闭合日 close/volume 以外的日频及5m历史查询、前后复权、基本面/PIT/因子、历史全市场股票池/指数成分、
基准收益、公司行动分钟入账、期货/基金/科创创业板/北交所、多个子账户、short、tick、
集合竞价、GTC/IOC 自定义有效期、周/月调度、任意文件 API、实盘、恶意代码沙箱。
已知 API 名称有显式 `JQ_API_UNSUPPORTED`；未知 jqdata 导入同样失败。未知 Python 全局保留 Python 错误。

## 最小 AD 输入

`ad-minute-backtest.v1` 是 AQ 消费契约，不是新的源采集服务。
示例生成器可查看完整字段形状。真实数据要求 `producer.name=ashare-data`，
并保留版本/提交/来源摘要、`source_refs`、`limitations` 和 `real_market_data`。

### 日状态的可见时间与显式事后回放

分钟可见性和日状态可见性是两个独立契约。`state_visibility` 省略时默认为
`declared_available_at`，保留原有严格规则：每个状态须有证据、真实声明的
`available_at <= 当日09:00`。未知/null 仍拒绝；打开任一个研究开关都不会自动改写
这种输入的模式。接收回执时间不等于历史首次可见时间，不得回填成 09:00。

如果 AD 只能提供对应日期的事后日状态，可在独立研究运行中同时声明以下数据模型，
并传 `run_backtest(..., accept_state_replay=True)` 或 CLI `--accept-state-replay`：

```json
{
  "state_visibility": "posthoc_daily_replay",
  "state_replay": {
    "model": "dated-daily-state-0900.v1",
    "purpose": "retrospective_research",
    "evidence": "AD 固定日状态范围及来源/字段证据引用",
    "limitations": ["历史首次可见未知；不是历史 PIT 或盘中状态认证"]
  }
}
```

每个 `states` 行仍需已知的上市、停牌、上下限和前收字段及 `evidence`，另须：

```json
{
  "security": "600000.XSHG",
  "date": "2026-10-09",
  "available_at": null,
  "historical_available_at": null,
  "observed_at": "2026-10-13T03:32:54+00:00",
  "replay_effective_at": "2026-10-09T09:00:00+08:00"
}
```

以上仅为字段片段，不是可运行行情包或所示证券日期的事实证据。
`observed_at` 是实际源回执；`replay_effective_at` 必须恰好为**该状态日期**的 09:00，
只代表模型生效点。原始 `available_at` 保持 null；不能同时放入伪造的历史时刻。
模型声明、开关、原始输入和证据均进入运行身份；预检和结果写出 `state_visibility`、
`state_replay_used`，`historical_pit_verified` 仍为 false。默认关闭，少了显式接受就阻断。

AD 证据必须证明字段所属证券、日期和日状态范围，并区分事实与条件价带模型。
当日未来行情计算出的指标、盘中变化的收盘终态，不能作为本模型的盘前日状态。
若状态在盘中改变或其适用范围未知，数据侧须报告缺口，不得借回放开关填“正常”。
任何缺失上市/停牌/价带值、缺分钟区间、未知行动覆盖仍按原校验阻断。
`is_st` 未知时 API 读取仍报错；不能由开关把 null 变成 false。

策略公开 API 只能读取当前日已到模型生效点的状态，不能读取未来日期状态；
分钟条继续按自身 `available_at` 门禁，未完成/未可见分钟不会因此开放。
如果分钟首次可见也未知，仍须独立声明 `visibility=assumed_closed` 并传
`--accept-assumed-visibility`；状态开关不能代替分钟开关。该研究模型不重建当时的
知识集合，不能用于认证历史 PIT、盘中无停牌或历史无前视表现；真实经济验收必须
同时保留这些限制与实际源证据，不能标成聚宽对齐。

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

官方参考：聚宽[API 帮助](https://www.joinquant.com/help/api/help)最初公开读取返回地区限制，
后续本次两例对照已通过获授权浏览器读取生命周期、订单和费用规则；
[官方 SDK](https://github.com/JoinQuant/jqdatasdk/blob/ba61143a6504bb1f797b16b8f9d23a21909c4f60/jqdatasdk/api.py)
仅提供数据函数签名参考。对照使用单独获授权的浏览器时段，未使用因子额度。
