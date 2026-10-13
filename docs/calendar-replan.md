# 真实日历计划的空状态迁移

当原13/14计划因基础设施失联而未产生任何意图或原生账本时，使用显式的有限计划迁移。该机制不清理已有账户，也不赋予交易权限。

`online_cli migrate-empty --state-sha <原文件SHA> --reason <原因> --plan <CalendarReplan JSON>`：

- 通过固定AD reader读取并验证其新行动范围、基线和交易所日历摘要；计划必须一致。
- 在 writer 锁下核对完整旧state摘要，要求days为空、head/last_day为空，且没有日期目录、原生检查点或request证据。
- 计划绑定旧scenario摘要、新owner pin、同证券集合和模型、两天相邻工作日、新行动基线、日历及来源状态清单摘要。只更新计划/owner/engine身份，资金、风险、费用、策略等经济条款保持不变。
- 保存逐字旧state及规范化新plan，再发表新身份和迁移事件。旧SHA重试、错误旧计划/owner/范围、非空账户或孤立执行文件拒绝。已有plan时，不允许省略新plan而静默换owner。
- 重读state检查plan摘要及owner/模型/证券绑定；prepare必须在计划两个日期内，空账户只能从第一个日期开始。observed v4包要求显式plan，且逐证券行动证明基线必须匹配。所有实际时间门、owner当前来源检查和原生资金/持仓风控保持原有约束。

新代码在独立工作树。真实`observation/online/state.json`继续旧身份、零意图/零成交；迁移先对隔离副本演练。整体独审通过后才可按明确旧SHA迁移真实空状态并启用14/15入口。若独审或来源未通过，则保持阻塞，不能用15日首开替代14→15接续。
