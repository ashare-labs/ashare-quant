# 组合和公司行动

`ashare_quant.portfolio` 是多证券日频运行时，`tasks.portfolio` 负责数据、策略、预检、队列与 HTTP。输入来自 A数达公开日频导出，`CorporateHistoryView.export_aq` 提供支持范围内的行动。输入验证摘要、覆盖、角色与来源标签。

```sh
ashare-quant --db NEW_DATABASE portfolio-data-import --input EXPORT.json --name sample
ashare-quant --db NEW_DATABASE portfolio-catalogue
ashare-quant --db NEW_DATABASE portfolio-preflight --request selection.json
ashare-quant --db NEW_DATABASE portfolio-create --request request.json
ashare-quant --db NEW_DATABASE task-worker --once
ashare-quant --db NEW_DATABASE portfolio-result JOB_ID
```

`covered_research` 需要行情、停牌/限价状态、行动覆盖和支付/股份日期；回溯状态须显式接受并确认假设。`price_only_no_limits` 是无价限、无行动的诊断模型，不能静默丢弃真实行动输入。

正式产品入口先查询已有行情库，再检查所选股票和日期的参考来源覆盖。`POST /api/v1/product/coverage` 接收 `securities/start/end/source` 和可选 `reference_set`；`POST /api/v1/product/releases` 使用相同选择，`mode` 默认 `covered_research`。覆盖缺口不触发换股票、换来源组或诊断模式。发布是可逆的本地操作，执行仍需确认组合预检返回的精确 `assumption_ack_sha256`。数据目录返回的 `mode` 和 `diagnostic_allowed` 来自输入，含正式公司行动的数据不能切成诊断。

普通发布分别保存主库原始价格、交易参考和公司行动。价格不由固定研究包替换；参考包只决定哪些证券日有足够证据。AQ 复用日期账户模型，把现金、送转到账和可卖日附入独立消费输入；原 AD 发布文件保持不变。只有资料确证范围内的条件研究可以通过：税前分红、开盘前到账日期模型、日线状态回放和有来源的派生价限均会写入假设。它们不证明历史盘前可见性、盘中状态、全市场行动完备性或实际执行许可。

部署清单的 product profile 可声明多个 `reference_sets`，并显式指定 `default_reference_set`。`daily_execution` 组引用 `research_asset/execution_asset/corporate_history_asset`；`historical_market` 组引用 `market_asset/corporate_history_asset`。对应资产 kind 为 `daily_research/daily_execution/historical_market/corporate_history`，path 指向只读包目录，binding 为包内相对文件（默认 `binding.json`），`binding_sha256` 固定包身份。前三类 role 为 `market_reference`，公司行动为 `corporate_source`。未配置参考包仍能查询价格，普通发布会返回准确缺口。

旧 CLI 也遵循 `--deployment/--profile` 或 `ASHARE_DEPLOYMENT` 的状态选择，API 和 `task-worker` 使用同一控制库。显式 `--db` 与部署库冲突时拒绝；未选择部署时，需明确 `--db` 或 `ASHARE_QUANT_DB`，不会在当前目录默建状态。已有任务恢复使用冻结输入、策略、检查点和回执；新发布需要所选来源包，完整备份同时保留这些小包及控制库同级 `inputs` 中的原始发布文件。

算法 ID：equal-weight、momentum、trend、weekly-reversal、daily-equal-weight、low-volatility、momentum-low-volatility。ID 是业务值，Python 文件用 snake_case。参数、窗口和策略源码都冻结到任务。

现金与股份由 RQAlpha 原生账户处理。到账、解锁、应收、待到账和受限权益分别保留；不能因可交易数量为零而丢掉权益。缺登记基数、未知事件、来源冲突和必要价限缺证继续拒绝。

公开回测任务以初始现金建立新空仓账户，不继承开始日前的分红或送转权益。登记日在窗口之前的行动仍完整保留在来源输入和摘要中；除息日或派息日买入，不会获得此前登记的权益。例如登记日为 4 月 7 日、除息及派息日为 4 月 8 日：4 月 8 日开跑的新空仓账户可以买入，但该笔分红为零；4 月 7 日开跑并在登记收盘时持股，才按登记数量获得权益。预检会声明本窗口的起始账户政策，执行前须确认其精确摘要。

底层 `research_seed_previous_close` 仅声明研究用初始持股，不代表外部登记记录或完整历史账户。该证券有非零 seed 且选中的行动登记日在窗口之前时，输入会拒绝并指出证券和登记日，不能用初始数量推算旧权益。登记日在窗口内时仍按原生收盘持仓登记。恢复已受理任务沿用原始窗口和完整权益日志；即使后来卖出至零，也不会丢弃已登记但尚未到账的现金或股份。

HTTP 前缀 `/api/v1/portfolio`，字段见 [生成契约](contracts/portfolio.openapi.json)。结果中订单、成交、账户与行动投影必须一致；验证实际账户守恒与恢复结果。
