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

算法 ID：equal-weight、momentum、trend、weekly-reversal、daily-equal-weight、low-volatility、momentum-low-volatility。ID 是业务值，Python 文件用 snake_case。参数、窗口和策略源码都冻结到任务。

现金与股份由 RQAlpha 原生账户处理。到账、解锁、应收、待到账和受限权益分别保留；不能因可交易数量为零而丢掉权益。缺登记基数、未知事件、来源冲突和必要价限缺证继续拒绝。

HTTP 前缀 `/api/v1/portfolio`，字段见 [生成契约](contracts/portfolio.openapi.json)。结果中订单、成交、账户与行动投影必须一致；验证实际账户守恒与恢复结果。
