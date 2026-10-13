# R7 完整分钟会话：本机复放

发行包为 `0.11.0.dev18`，服务身份标签沿用已审源码的 `0.11.0.dev15`。服务同时绑定源码 SHA256；Web 任务页固定这三个身份字段，不以版本标签单独接受新执行器。原任务库须保留原执行器，不迁移到本版本。

本增量消费 AD `684008c80d9a14e6df69eff8f5e101be42b4b538` 已发布 wheel 的公共导出。导出与原 R5 审阅件同字节，仍为 `tdx-full-session-retrospective.v1`：960 条执行分钟、480 条只读预热、4 个开盘代理、4 个收盘估值、6 个日汇总。输入继续保留原来源、零量行及未知时钟。AQ 原生环境仍依赖固定 AD `0.8.0.dev2`；新的 AD 导出在独立环境生成。

## 已准备的本机入口

在实际 AQ main 根目录执行，`AQ_R7_EVIDENCE` 可改为同一合法交付的其他位置。输出目录必须是新的，程序不会覆盖已完成产物。

```sh
cd ~/Workspace/ashare-quant
export AQ_R7_EVIDENCE="$PWD/.verification/jq-minute-r7-integration"
export AQ_R7_BUNDLE="$AQ_R7_EVIDENCE/delivery/jq-minute-real-session-r7"
.venv/bin/python -B -m examples.jq_minute_comparison.session_compare \
  --input "$AQ_R7_BUNDLE/market/full-input.json" \
  --ad-export "$AQ_R7_EVIDENCE/published-ad/session.json" \
  --inherited-input "$AQ_R7_BUNDLE/market/old-input.json" \
  --jq "$AQ_R7_BUNDLE/reference/jq" \
  --gap-reference "$AQ_R7_BUNDLE/reference/gap" \
  --output .state/jq-r7/my-three-cases
```

入口运行 `shared_cash_t1`、`visible_momentum` 和 `gap_ported`，使用公开 `run_backtest`。JQ 记录仅在执行后参与对照，不注入行情或原生账本。完整移机复放使用交付包的 `REPRODUCE.sh`，显式传入两个已准备的隔离环境：

```sh
AQ_PYTHON="$PWD/.venv-r7-verified/bin/python" \
AD_PYTHON="$PWD/.venv-r7-ad-export/bin/python" \
  sh "$AQ_R7_BUNDLE/REPRODUCE.sh" .state/jq-r7/full-replay
```

该命令重新校验私有包、AD 导出、三例、真实边界、358 项分钟回归及日频九例。环境和私有输入均不随源码或发行包分发；缺少这些材料时按原交付手动准备，不联网补数。

## 标准服务启动

```sh
.venv/bin/ashare-quant --db .state/tasks-v6-jq-r7/control.sqlite3 serve --port 18819
```

这是新的任务控制库路径。原固定模拟 runner、pins、数据库和 paper 入口继续使用各自封存环境。服务仍为 16 个 scoped HTTP 路径的 `m2-jobs.v6`；分钟研究入口不扩展任务服务的业务范围。配对 Web 使用同一源码摘要及服务标签，端口占用时另选空闲端口，不停止其他服务。

## 验证与边界

三例的 305 条原始观察、132 个账户快照和 8197 个直接相等叶字段通过原独立只读核算；source/wheel 的输入、策略、manifest、结果及事件流相同。真实收盘估值解释原两例日终差；旧分钟十组矩阵和日频九例的 296 笔成交保持原结论。全部金额及原始 JQ/行情证据只供本地验证材料引用，当前文档不分发原始记录。

终端 `get_orders/get_trades` 查询集合为空仅适用于显式 `jq_minute` 的 end 阶段；15:30 集合、原生账本、持仓和既有挂单政策保留。09:30 是事后开盘代理，三份实际脚本未在该时刻新下单。旧严格分钟题、历史 PIT 和全市场资格不升级。

未验证的范围包括多旧挂单同刻容量共享、相等触价或仅高低价穿越、卖单对称、非零滑点、开盘代理成交、平台零量新单处理、11:30/15:00 内部阶段及精确撤单时刻。源量、未认证涨跌停和对象字段差异继续明列；不改变源量来追平平台。日量作初始容量分母是显式 non-PIT 角色，普通策略不能提前读取当日日量。

冻结来源、独审及本地验证清单见 [整合记录](jq-minute-r7-integration.json)。
