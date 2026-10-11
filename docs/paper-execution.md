# AD 单日交付与原生分红续接

本实现消费固定 AD `dc0198bc956e8eb658da18f59ce02b8cf7bc37c0`（消费 API 0.9.1.dev13，数据生产版本0.9.1.dev12）及 AQ `90f6ce99076b456bc8cbf3d055e531d2c3f0683a` 的分红决策交接。2020/2022 是明确研究模型下的整窗准入；2024 仍缺四只沪市证券58日的ST、涨停和跌停字段（232个证券日，696个字段）。更新中的AD工作树不参与读取。

## 固定来源

| 对象 | SHA256 |
| --- | --- |
| 研究 binding | `174450952a5f1dd7491c4e3dc059440aae605f25bdf1b260057fec3f039ac310` |
| 执行 binding | `c7f05ea4b9079bd19576d17ff2e0a7bf07569df9bb86ac587b6f563b6833368b` |
| AD dev13 wheel | `5b3c8051560519eefa33f91c37c9f918d5a9c599994b6ffc69171d9e8d71941d` |
| r1消费契约 | `d09c4f6fc52f2184d6ade145c0375d0e108ffd271ef5db8e4536ca6cde0654c1` |

`bind-execution --consumer-contract <r1契约> --consumer-contract-sha <固定sha>` 校验消费契约、bindings、清单文件及wheel摘要，复制进运行目录并固定模型选择。两个数据binding未变。绑定时实际解析格力研究 `/actions/3` 和执行 `/actions/0`，核对证券、日期、现金、送转和参考扣减；将包根 `reports/exact-gap-scope.json` 与执行数据中的null单元逐项比较。证据清单不代替数据binding。AD公共读取器在隔离无网络子进程运行；原AQ/RQ锁定环境不升级。每次读入再校验AD可执行文件。

显式选择：AD 的 retrospective_daily_status、shared_market_calendar、standard_mainboard_limits、scoped_action_absence、gross_cash_zero_withholding、dated_name_history_status，以及 AQ 的 scoped_listing_continuity 和 gross_zero_withholding_0900。`execution_permission=false` 表示没有实盘权限。整窗准入、空订单观察准入和证券/日期/方向单笔准入分别记录，不能互换。

## 实际已消费的字段与边界

| 输入 | 处理 |
| --- | --- |
| D-1 历史 | AD public history 09:00 查询的 120 个收盘价；逐日核对日历、最后日期、无 D 日或未来值 |
| D 日状态 | AD public signal_research导出的ST、停牌、上下限来源/推导/不可用envelope；观察保持null，实际目标订单再调用order_state；不用当前名称替代历史ST |
| 可交易范围 | 源上市日期和范围内连续性模型，明确接受后才设 eligibility |
| D 日开盘、收盘和估值 | opening 09:30、closing 15:30、history 15:30 的 OHLCV，交叉核对日期和价格 |
| 公司行动 | 公告、登记、除息、支付日期，毛权益与除息参考扣减分别保存；只支持无送转的现金分红 |
| 来源时刻 | `historical_available_at`、`payment_time`、`cash_available_at` 的 null 原样保留；不填成模型时刻 |
| 实际输入接收 | 本地实际 UTC `received_at`，独立于历史研究时钟；不声称这是当年的源送达时间 |
| 增量控制 | 单日输入 SHA、前一输入 SHA、所依赖的原生父检查点、公开 AD 读取收据和返回摘要 |

源是有限封存历史包。该入口证明单日交付、恢复和原生账户续接，还不是在线行情供给或真实持续模拟验收。未知的历史送达、支付分钟级时刻限制可作的验收声明，不要求先补齐全市场历史 PIT 才能继续这些明确模型的样本。

## API 与恢复

现有 `paper-api.v2` 增补 `POST /api/v1/paper/sessions/{sid}/ingest`，请求 `{day, expected_parent, reason}`，响应 `{day, packet_sha256, received_at, already_received}`。随后通过原 `/advance` 推进原生账户。公开输入不能自选股票子集、方向或行情；错误日期、跳日、额外证券字段和冲突重复请求均拒绝。

每次只允许接收固定下一日；上一接收日须已原生提交。缺日、乱序、父检查点不同或未满足全局模型/覆盖均拒绝。状态未知在观察阶段保留为告警，不拒绝整个观察日。输入文件落盘但尚未登记时重试会重新读取AD并比较后入库；登记成功但响应丢失时重试返回同一收据。SQL仅保存摘要与控制事件，没有账户余额或记账权。冻结输入被改动时拒绝继续。

生成本日目标后，根据上日原生数量求真实增减仓方向，调用AD `preflight_orders` 和每个目标订单的 `order_state`，保存当日许可。封存意图时重新计算核对；原生提交每笔订单前再次核对许可的日期、证券、方向、状态及限价字段。其他日/方向的许可和历史订单影响报告不能复用。AQ原有禁ST、现金/费用、T+1和原子日意图约束继续生效：某个实际目标订单缺必要字段时，整份日意图在原生账户提交前拒绝，不删掉订单或证券来制造成功。无订单观察日可正常日结。

2024仍有696个未知字段。固定参考三策略受影响订单8/17/45只是历史影响统计，5%/10%敏感性一致不授予订单或整窗许可。趋势策略可先续接没有未知目标订单的日期，遇到实际所需字段未知即停止。

原生 worker 每天只跑 D 日并载入上日检查点，事件游标每次加 4、日结加 1。每日策略收据中的交付列表固定截至D日；接收或提交后续日期后，用原父检查点重试D日仍返回相同完整报告，当前账户head不回退。实时状态页继续显示最新输入列表。SIGKILL 后先显式 resume，再恢复相同冻结日。没有每天重跑整个历史窗口来伪装续接。

## 现金分红

`SessionConfig.cash_dividend_policy` 默认 `reject`，显式研究政策为 `gross_zero_withholding_0900`。AD `pay_date_before_open` 映射此政策；15:30 政策不兼容当前原生开盘前生命周期，明确拒绝。登记日必须是除息前一交易日，其他时序暂不支持。

现金行动进入 RQ 原生数据源的 dividend 事件，由原生 StockPosition 调整成本、产生应收、支付时转为现金；持仓为零仍保留未支付应收。AQ 无手工现金加款、第二账本或研究参考余额覆盖。独立对账只读原生开盘前后状态：登记数量/检查点、成本变化、应收新增/结清、现金增量、持仓数及现金+市值+应收=NAV，误差限 0.000001 CNY。

2022-04-07 登记的格力 3500 股在 04-08 按每股 1 元毛权益原生入账 3500 元。源除息参考扣减 0.936124 用于价格规则，不替代投资者现金权益。支付时刻09:00、扣税0是冻结研究模型，真实到账时刻仍未知。

JQ 独立参考净额2621.08元与本模型仍差878.92元，其中223.65为毛额口径差，655.27为从JQ日志毛净额推得的扣减；没有独立税款流水证明，不能称其为已核实税额。该差异保留，不改源金额或原生账本去对齐。

## 复现

在已锁定 AQ Python 环境中：

```sh
PYTHONPATH=src /path/to/native/python -B scripts/prepare_paper_execution_review.py \
  --execution-source /path/to/sealed-ad-v13-r1 --output /path/to/new-review
PYTHONPATH=src /path/to/native/python -B -m ashare_quant.paper \
  --root /path/to/new-review/store serve --port 18818
```

准备脚本创建独立空账户并逐日运行至登记日，不复制其他账户余额。`ready.json` 给出会话和父检查点。前端接收04-08后才允许推进，也提供2024缺字段、无订单观察和保守风控账户。自动浏览器验证和独立人类点击分别记录，后者未发生不阻断代码交付。

全矩阵入口为 `scripts/verify_paper_research.py`，附 `--execution-source /path/to/sealed-ad-v13-r1` 消费新AD日包；不附该参数、显式指定 `--cash-dividend-policy gross_zero_withholding_0900` 则验证原固定历史导出九组回归。后者2024结果不升级为新AD执行用途准入。测试 `tests/paper/test_execution_delivery.py` 使用 `ASHARE_PAPER_AD_PIN=/path/to/sealed-ad-v13-r1`；未给固定包时明确skip，不下载或改造数据。
