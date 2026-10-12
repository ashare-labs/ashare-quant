# 前瞻日线候选：在线连续验收仍待完成

`paper.observation` 消费 AD `ad-current-daily-observation.v1` 固定收据，使用原有 `daily_portfolio.signals.decide('equal-weight', ...)`。范围为原池中三只沪市证券，计划初始资金默认100万元。该资金是尚未建账的候选参数；输出账户现金/持仓/NAV均为null，订单和成交为空。

```sh
PYTHONPATH=src python -B -m ashare_quant.paper.observation --root /独立候选目录 freeze \
  --ad-python /固定AD环境/bin/python --ad-source /AD源码/src \
  --ad-store /观测目录 --capture 完整SHA256
PYTHONPATH=src python -B -m ashare_quant.paper.observation --root /独立候选目录 status
```

该CLI目前面向源码工作树（会核对现有 `examples/daily_portfolio/manifest.json` 原证券池），不升级原生AQ所安装的AD。AD在独立子进程只读取固定本地证据，源码清单摘要绑定候选计划。

候选只面向源日历下一开市日：09:00决策、09:05截止、最早09:30模拟成交。D日晚间收到的日线可准备D+1候选，不能生成D日早盘订单。实际收到时间在决策之后，或首次冻结时已过截止，均拒绝。相同经济输入重投返回原完整候选；改价、修订或更换策略/AD源码拒绝。候选按日索引和内容摘要原子提交，原文损坏明确拒绝，进程重启/提交后SIGKILL不产生第二份候选。

此入口是在线运行的前置检查，不是在线撮合实现。现有 `PaperService` 原生单账本和历史接口保持原样；`observed_data_enabled`/`continuous_paper_accepted`保持false。缺当日及时状态、限价及公司行动覆盖，另缺已验在线原生接口，因此候选始终 `execution_admitted=false`，CLI退出2。status按实际当前时间显示等待证据或错过窗口，不以永久的旧状态文件冒充当前健康。

接续实现须在真实09:00之前取得当日证据并冻结订单意图，之后才消费09:30以后撮合价格；仅有收盘后日线不可追认当日交易状态。提交原生账户前还需复用现有独立风控、幂等槽位和原生父检查点；禁止把候选改名为历史回放来获得成功交易。首批至少实际观察两个相邻交易日，并保存同一原生账户的现金/持仓、接续检查点和真实源时刻，才能另行讨论在线验收。
