# 完整分钟生命周期与显式容量模型

默认行为保持 `lifecycle="closed_bars", capacity_policy="strict_minute"`。
新模型由已冻结的真实 R4 平台观测约束，工程覆盖包括可运行原脚本、完整时钟、
跨段挂单、零股尾单、T+1、共享现金、独立现金分项审计和失败恢复。
dev18已用固定AD公开导出完成三份真实脚本的离线聚宽对照，见
[完整会话真实验收](session-results.md)。工程边界与平台实证范围仍分别标明。

## 选择和入口

已有公开 `run_backtest` 和 CLI `--config` 接收以下设置，所有选项计入运行身份：

```python
config = {
    "lifecycle": "jq_minute",
    "matching": "jq_current",
    "capacity_policy": "jq_daily_initial",
    "cash_policy": "resize",
    "volume_percent": 0.0001,
    "initial_cash": 10000000,
    "commission_rate": 0.0003,
    "min_commission": 5,
    "sell_tax": 0.0005,
}
```

`jq_daily_initial` 要求同时启用前两项，并要求分角色 AD 会话输入。
`jq_minute` 可以单独配合 `strict_minute`，此时仍是共享整手分钟容量，
开盘无闭合条时 `jq_current` 新单仍显式拒绝。模型混合不会暗中改变容量。

## 回调与数据时钟

- `initialize`：首日 00:00；数据与下单仍不可用。
- `before_trading_start`：每日 09:00，然后执行同刻注册回调。
- `run_daily`：09:30–15:00 每分钟、09:00、15:30，以及 `every_bar`。
  11:30、12:00、15:00 可定时观察，不允许这些时刻提交新单。
- `handle_data` 和 `every_bar`：09:30–11:29、13:00–14:59，各120次。
  同刻先执行定时回调，再由原生 broker 处理旧挂单，最后执行分钟回调。
  09:30、13:00只有回调事件，不造行情 BAR；午休没有撮合事件。
- `after_trading_end`：15:30，在该时刻定时回调之后。
- `on_strategy_end`：末日23:59:59.001；异常使整次运行失败，不产生完成回执。

原始历史分钟和执行分钟分区。首日前的条只能供历史查询，不触发旧日回调、订单或成交。
开盘代理仅含 AD 提供的一个价格值，不能使首分钟 high/low/close/volume 提前可见。
dev17起，09:30下单必须有当日已可见开盘单值；缺失或迟到时显式失败，不能用昨收成交。
13:00下单必须有当日已可见的上午闭合条，不能退回开盘代理或昨收。
`last_price`及持仓估值的昨收参考不构成下单价格资格；已有可见闭合条时不依赖迟到的day_open。
收盘估值记录只更新原生 Position.last_price，不生成额外撮合或成交。
已闭合日 `get_price` 支持 `close/volume`、单股或多股、日期范围或 count，仍要求 `fq=None`；
盘中读取当日日量被可见时刻门禁阻断。5m、复权、动态证券池等未增加支持。

## 显式事后容量与原生账户

`jq_daily_initial` 是研究撮合政策：新单每单独立使用当日最终成交量乘参与率，向下整手；
同股同时两单各有该额度。后续已闭合分钟的旧限价单，每单使用该分钟量乘参与率向下取整股，
允许1股等部分成交。同一订单同一时刻只尝试一次；不把一笔 BAR 多次使用为追加额度。
新单价是已可见当前价，旧限价单按严格穿过 low/high 的委托价撮合。

已可见分钟的源量为零时，新市价单取消、限价单继续等待，旧挂单也不成交；
回调及可见历史保留，下一条有量记录再按声明政策处理。即使初始额度来自日量，
也不能在一个已知零量分钟借日量产生成交。此处是显式保守执行政策，尚未新增平台实测。

以下规则的真实证据范围分别保留，不能从单一场景推广为平台事实：

| 规则 | 冻结R4的证据 | 当前状态 |
|---|---|---|
| 初始同股同刻每单额度 | 两个市价单各9400股，与日量整手模型相符 | 事后模型，有有限支持；精确分母未唯一识别 |
| 后续整数股分钟量 | 10/89/86/127股与公式相符 | 仅该样本支持 |
| 后续多挂单各自额度 | 每时点只有一个该股旧挂单 | 逐单是模型选择，平台未验证 |
| 相等触价、low/high穿过但close未穿过、卖单对称 | 只有买单正穿价 | 严格穿价/卖单对称为模型选择，平台未验证 |
| 11:30/15:00内部到账阶段 | 定时观察后到下一观察前发生 | scheduled后BAR处理是实现假设 |
| 零量分钟新单处理 | 未在零量分钟提交容量新单 | 禁止成交是本地政策，平台未验证 |
| 精确日单撤销时刻 | 15:30未撤、次日09:00已撤 | 盘前回调前清理是模型边界 |

这些未验证项也保存在 `result.execution_policy.unverified_jq_rules`。

日终分母在盘中使用是**显式事后模型**。它不用于策略选股、历史或当前数据，不认证可实盘性，
也不与默认严格分钟容量混为一谈。精确有理数取整避免浮点将99.5股提升为100股。

复用 `SimulationBroker`、原生 Trade/费用/T+1，以及仓内已有 `BudgetAccount`。
买入限价单冻结余额按初始委托预算减实际成交金额与已付费用，保留成交改善后的剩余预算；
未另建成交或现金账本。`cash_policy=resize` 仍通过原生风险与金额下单接口处理共享资金。
独立审计分别重建 available/locked cash；两者相抵、总现金不变的篡改也必须失败。

R4只证明15:30仍有挂单、下一交易日09:00已无挂单，**未证明精确撤单时刻**。
本地声明 `before-next-session-callback`：下一日09:00用户回调前经原生 cancel 释放。
末日无下一交易日事件，挂单及冻结余额保留到最终快照；不把这个边界称为平台真实撤单时间。
完整恢复仍仅支持已完成结果校验重入，不恢复任意 Python 中途状态。

## 对象投影与迁移

仅 `jq_minute` 改变脚本对象投影：当日 `get_orders/get_trades`、Trade.time、Position.today_amount、
Order.commission，以及真实参考中观察到的状态标签和三位小数成交均价。
全部成交订单标签为 `held`；仍挂着的已部分成交限价单为 `filled`；是否仍挂单由
`get_open_orders` 判断。原生事件/结果保持原生状态及委托价，额外 `limit_price` 保留委托价。
卖单 avg_cost 取提交时持仓成本，买单取成交均价。未成交对象、均价舍入临界点、
卖单在平台的零股部分成交等未新增真实实证，不宣称全对象兼容。

dev18按两份真实参考修复终端查询：`on_strategy_end`中的`get_orders/get_trades`为空，
15:30的当日集合仍正常可读。此处只结束会话查询视图，不删除原生订单、成交、持仓或结果。
未成交末日挂单仍遵循上述保留政策；真实参考没有末日仍挂单的终端查询场景，不能据此认证它。
额外的AQ Trade.at/side/commission/tax和LimitOrderStyle公开price与JQ私有`_limit_price`
不声称逐字段一致，比较报告逐项保留这些接口表示差异。

[`gap_probe.py`](gap_probe.py) 保持已上传真实源码字节不变。
[`gap_ported.py`](gap_ported.py) 只在分钟历史调用增加 `fq=None`，其余字节相同；
不会静默把聚宽默认前复权当成原价。原来的两份策略仍按旧默认模式回归；
不同资金政策下原脚本的 AQ 专属断言可能不成立，既有观察版将其结果明确记录。

## AD 输入和证据责任

新消费版本 `ad-minute-backtest.v2` / `aq-tdx-minute-research.v2` 使用
`session_observations.schema=ad-minute-session-observations.v1`。
包含 `opening_prices`、`closing_marks`、`daily`、只读 `history_minutes/history_sessions`；
角色、来源标签、单位、证据、观察时刻与可见时刻须由 AD 明示。
研究数据保留 `available_at/historical_available_at=null`，另列 `replay_available_at`。
旧v1不能偷偷附加这些记录；缺覆盖、重复、未来收盘提前可见、身份或角色错误均拒绝。

完整研究 profile 只准入原已审计的两股两日，保留历史PIT、限价与公司行动不完整限制。
收盘竞价来源条可在显式研究时钟中参加旧单撮合，不宣称重建交易所竞价撮合队列。
新真实输入必须来自 AD 公开导出；AQ 不把旧审计用1440行自行提升为可执行契约。
供应商数量精度差保持原值，不能修改价格或成交量迎合平台输出。

dev17的完整研究输入允许**带AD明确收盘阶段声明的原始零量条**：`volume=0`、`amount=0`，
source_label在14:57之后至15:00，`session_phase="closing_auction"`；`phase_evidence`包含
`model="ad-declared-closing-stage.v1"`、同一source_label和manifest_sha256、非空source_refs、
`historical_phase_verified=false`。这是绑定原始记录的研究角色，不把未知历史阶段认证为事实。
该角色须由AD公开导出投影，测试里的角色声明不能代替AD交付。
缺角色、连续交易零量、量额矛盾或来源不匹配仍拒绝；旧v1研究模式仍要求正量额。
完整会话覆盖不变，既不能删14:59，也不能填非零量额来通过；原始未知时钟继续为null。

本地结果持续写 `jq_comparison=NOT_PERFORMED`。真实比较在独立报告中绑定 AD 输入、
平台源码/日志、固定 AQ SHA、运行配置及全部分项核算；只有实际跑过的范围才能宣称一致。
