# 连续交易日持有接续

`ashare_quant.paper.successor_cli` 在一个已完整结算的原生账户上追加下一交易日。
不创建替代账户、不补回已撤销订单、不重新运行前日策略。
日期取自原生决策及 AD 日线包共同确认的 `next_open`，同时验证有序交易日列表中不存在跳日。
因此周五可以接周一，节假日也按来源日历处理。持有策略遇到月度再平衡日会明确拒绝，
不会将再平衡伪装成持有。

新绑定记录上一状态全文摘要、所有既有账本文件摘要、原生收盘检查点、报告、游标和结算次数。
每次扩展单独授权一个交易日；无需将首次 `base_cursor=4` 的限制延续到后续日期。
旧单日接续及运行时授权原样保存在内容寻址的 `successor-origins/` 状态快照中。
后续运行核对整个继承链、原生结算证明及既有文件。原账户身份、初始配置和过去的成交记录不改写。

运行时源码、AD 读端和行动审阅基线的变更均需显式绑定。Python 和原生 RQAlpha 依赖必须一致。
AD 新基线必须具有前基线到新基线的审阅链；仅切换环境变量不能使旧计划接受新事实。
构建计划和绑定是前台命令，均不采集盘中报价。

```sh
python -m ashare_quant.paper.successor_cli \
  --root ACCOUNT --ad-source AD_SRC --ad-python AD_PYTHON \
  --daily-store DAILY --phase-store PHASES \
  --runtime-sha REVIEWED_NEW_RUNTIME --from-runtime-sha PREVIOUS_RUNTIME \
  build-plan --start-at 'YYYY-MM-DDT09:35:00+08:00' --output PLAN

python -m ashare_quant.paper.successor_cli \
  --root ACCOUNT --ad-source AD_SRC --ad-python AD_PYTHON \
  --daily-store DAILY --phase-store PHASES \
  --runtime-sha REVIEWED_NEW_RUNTIME --from-runtime-sha PREVIOUS_RUNTIME \
  bind --plan PLAN --plan-sha PLAN_SHA --state-sha CLOSED_STATE_SHA --review-sha REVIEW_SHA
```

绑定使用现有全局账户锁和控制锁。重复的相同绑定只返回原结果；中断恢复不会重复追加绑定事件。
开盘、收盘、恢复继续使用 `intraday_cli`，所有原有时间门禁、风险、分钟因果、公司行动及幂等检查保留。
原账户第一次启用前应在完整副本验收；测试的未来时钟和合成来源不能作为真实跨日验收结果。

`tests/paper/test_calendar_successor.py` 接受 `ASHARE_SUCCESSOR_ACCOUNT` 指定只读的已结算账户，
只复制到 pytest 临时目录。可设置 `ASHARE_SUCCESSOR_OLD_SOURCE` 验证先前运行时拒绝新绑定。
它测试第三日、跨周末、执行与结算中断、重复投递、离线重读、错误日历、修订数据、游标重置和撤单余量重引入。
