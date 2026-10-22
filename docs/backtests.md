# 分钟与合成回测

分钟入口 `python -m ashare_quant.backtests.minute --help`；组合入口 `python -m ashare_quant.portfolio --help`。输入保留原始来源、时间语义、零量记录、预热及执行域；研究导出不能变成实时观察证据。

examples/minute/make_fixture.py 生成明确合成的最小分钟输入；equal_weight.py、limit_and_exit.py、rotation.py 是可信示例。examples/minute_research 是可导入的 Python 示例包。对照验证器位于 tests fixtures 或 scripts，产品不导入它们。

分钟运行保留原生撮合、T+1、费用、共享现金与预算预留。预算在 execution.reservation，根据未成交量及已发生费用冻结资金，不重复实现成交账本。检查点、隔离和 RQAlpha 内容验证在 execution.rqalpha。

所有分钟策略回调在独立的 50 位 Decimal 精度上下文内执行，避免上游首次导入降低
全局精度后使相等均线产生假信号；读取原生运行身份也不改变调用方的小数上下文。
新交易日的首根可用分钟柱出现前，账户使用该日已声明的前收盘观察估值，不沿用前一日
截断分钟窗口的末柱。该前收盘仍是估值参考，不是新鲜报价或成交价格。

## 通用有界分钟研究

AD 的 `export_minute_research` 输出 `ad-minute-research.v1`；AQ 使用
`MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)`
预检，再把同样三个显式开关传给 `run_backtest`。三个开关分别接受模拟收盘可见时间、
事后逐日状态重放和研究条件。预检通过不代表严格执行、历史 PIT 或聚宽对齐。
旧 `aq-tdx-minute-research.v1/v2` 保留原有固定案例规则。

新 schema 按能力校验，不含日期、个股或供应商名单。默认能力支持 20 只以内沪深主板股票、
最多 60 个连续交易日，显式前一日及后一交易日。逐日 listed=true、suspended=false、
is_st=false 与首次上市日期必须有来源观察；首次上市不足 30 个自然日不支持。停牌和未知
状态明确拒绝，不能当作缺分钟补齐。全部分钟必须覆盖所声明的现金交易时段。

`research.profile` 为 `source-labelled-closed-bars.v1`，`purpose=retrospective_research`；
`strict_execution_admitted/interval_verified/price_limit_verified/historical_pit` 均为 false。
`research.assumptions` 必须逐项采用
`ashare_quant.backtests.minute.research.ASSUMPTIONS`，`envelope` 为
`raw-previous-close-4pct-inward-cent.v1`。该 4% 内向包络是执行限制，不能作为交易所涨跌停价。
所有真实限制字段保持 null，源价格或订单触碰包络即拒绝。

| 输入 | 必需证据与约束 |
|---|---|
| `source_refs.evidence` | 名称 → `{source, sha256, ...}` 来源注册表；各 source_refs 必须引用存在的 SHA256 身份。摘要不认证上游真实性或独立性 |
| `research.calendars` | 按实际涉及市场的 `{days: {日期: bool}, source_refs}`；覆盖前后继之间每个自然日，开市日恰好等于 calendar |
| `states` | 每个证券日一行；source_observation 提供 listed/suspended/is_st/previous_close/first_listing_date 的 value、source_observed 状态及来源引用；历史可见时间 null，replay_effective_at 为当日 09:00 |
| `previous_close_observation` | 同证券、真实前一交易日、raw close 与状态前收一致，有来源引用；不是凭空假设的除权参考价 |
| `minute` | AD 原行、来源合同和行摘要绑定；六字段、标签、quality 不变；历史区间与可见时间 null，模型区间单独记录 |
| 零成交分钟 | volume/amount 同为零，仅限 14:58–15:00 显式 closing_auction；phase_evidence 使用 source-labelled-closing-stage.v1、同标签/源行摘要、historical_phase_verified=false 和来源引用；保留零行且原生容量不允许成交 |
| `action_coverage` | bounded_sources_no_known_overlap，complete/absence_proved/historical_pit=false；每证券有完整返回查询、全部事件及来源，窗口覆盖前一日至末日；每事件显式列 record/ex/payment/share_credit/new_share_trading 五日期，未知为 null，任一已知重叠拒绝 |

公司行动中查询返回完整并不等于所有公司行动已知；遗漏、未知日期和历史可见性继续保留为
风险与研究假设。事件查询使用 `queries` 列表，每项包含 security、query_complete=true、
start/end、source_rows、events、source_refs、milestones_checked（按上述顺序）以及
unknown_dates_are_not_no_event=true。`source_rows` 必须等于 events 长度。缺失事件键不能
解释为无事件。状态观测还需声明 dependency_conflicts=[]、intraday_halts_verified=false。
`source_quality.execution_permission=false` 与非空 limitations 保留查询完整性、传输完整性
等来源限制；source_observed 仅表示观察到来源字段，不是认证其真实性或严格执行可用性。

`research.capabilities` 必须明确 matching=next_open、lifecycle=closed_bars、
capacity_policy=strict_minute、initial_positions=flat、terminal_positions=flat。
运行配置偏离这些能力会在创建运行目录前拒绝。匹配、账户、T+1、手续费和现金预留由原生
引擎执行；无另一套替代账本。该 schema 不提供开盘独立观察、JQ 日量容量或任意 Python
断点恢复。完成目录按内容身份与产物哈希重入不重复交易；未完成目录保留并要求新目录重跑。

可复验的完整合成构造和故障注入见 `tests/minute/test_research_contract.py`；真实源资料
作为外部证据交付，不进入产品 wheel。接入新来源应在任务目录完成来源验收、量价/精度核对、
状态与事件审查，再由 AD 公开导出；修改摘要或勾选开关不能替代这些审查。

### 连续窗口与现金分红研究模型

显式选择 `source-labelled-corporate-date-model.v1` 才可扩展到 512 个连续交易日
（含前后继的自然日日历不超过 740 日）；原有 profile 的 60 日、4% 包络和无行动限制保持不变。
仍要求已上市至少 30 日、非 ST、无日级停牌的主板股票，首尾空仓、next_open、
closed_bars、strict_minute，以及全部三个研究开关。不能借此接受未知停牌状态或盘中权限。
该 profile 使用 `CORPORATE_ASSUMPTIONS`，`envelope=daily-preclose-mainboard-5pct-10pct.v1`，
capabilities 另声明 `corporate_actions=source_scoped_cash_dates`。

每个 state 必须携带 `daily_limit_model`：schema 为上述限价模型名，source_row 保留
同证券日原始日线 preclose、tradestatus、is_st、source、observed_at，绑定 source_row_sha256
和 source_refs，并明确 exchange_limits_verified=false。模型按 preclose 的 5%/10%、
分四舍五入构造研究阈值；目前准入仍仅非 ST，实际使用 10%。真实 reference_price、
upper_limit、lower_limit 仍为 null。源 OHLC 与模型的半分一致性容差只处理存储精度，
不会修改源价格或放宽订单阈值。新日 09:00 使用原始前收维持权益转移基础；09:30 起若无
当日分钟观察，使用显式源 preclose 模型，估值状态为 daily_reference_model。

`corporate_schedule` 保留 AD 的完整来源范围响应，`corporate_account_model` 为现有
`aq.corporate-date-model.v1`。action_coverage 声明 source_scoped_corporate_date_model，
绑定 binding_sha256、response_sha256 和 evidence，complete/absence_proved/historical_pit
均为 false。复用日线原生 CorporateStockPosition：登记日收盘数量确定权益，除息日调整
成本与应收，支付日开盘前到账；清仓后仍保留未到账权益。仅支持现金分红，送转股明确拒绝；
窗口前登记且初始空仓的行动不产生本账户权益。结果保留权益基数、金额和各记账阶段日期。

来源绑定的 volume=amount=0 分钟可保留在任意声明时段，原生成交容量为零，市价尝试取消；
零量本身不证明停牌、交易阶段或报价新鲜度。保留的价格只作非认证研究估值。
负量、量额冲突、未知状态及来源明确禁用仍受原规则限制。长窗口估值按当日索引查询，
不在每次账户观察时重扫全部历史。合成边界验证见 `tests/minute/test_corporate_research.py`。

冻结午休成交容量案例的复验入口是 `python -m tests.fixtures.minute_reference.audit_capacity_regression --help`。它核对外部案例、策略、输入和原生结果哈希，在新目录重跑并审计分钟容量、现金差额及成交变化。该入口限定既有 `R09-lunch-participation` 案例；历史 ID、`aq-minute-r09-capacity-fix.v1` schema 和证据哈希保留，名称不表示支持任意案例。所需历史输入不随包发布；案例和基线只读，结果只写显式指定的新目录。

`ashare-quant demo --engine fixture-replay-v1` 是确定性合成演示；native extra 可选 `--engine rqalpha`。这些场景验证控制层和原生约束，不代表真实行情回测。输出必须写新目录。

## 性能复验

确定性多证券长窗口基准、加载与引擎分层计时、完整事件一致性比较及当前测量见
[分钟回测性能基准](backtest-performance.md)。生成输入和运行产物写独立新目录，不随源码发布。
