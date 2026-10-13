# 两例真实聚宽分钟对照

后续 `0.11.0.dev15` 已在独立分支增加严格容量修复和显式执行选项。
以本页同一份聚宽原始日志离线复验，新模式两例逐笔成交及最终现金已对齐；
默认研究模式保留。实现、使用方式、R09 前后账本和未对齐项见
[执行语义闭环](execution-policies.md)。下面保留 `94a5e18` 对照基线的原始结果。

2026-10-13 在聚宽服务端实际执行 `shared_cash_t1`、`visible_momentum`。
两例的下单计划、提交时刻、读取的五根历史分钟及动量选择相同；最终收益不相同，
全部差额可按实测撮合时点、资金不足时的订单处理和费用解释。
结论是 `REAL_JQ_OBSERVED_MODEL_DIFFERENCES_EXPLAINED`，**不是严格等价或全 API 兼容**。
本批不改 AQ 引擎、数据输入、任务服务、数据库或旧恢复契约。

## 范围和结果

基线 AQ `14d8dfe7d4b79343162a5f3877613bdd44ac58b3`；源策略为同仓
[`shared_cash_t1.py`](../tdx_minute_research/shared_cash_t1.py) 和
[`visible_momentum.py`](../tdx_minute_research/visible_momentum.py)，源文件未改。
证券为 `600000.XSHG`、`000001.XSHE`；2026-10-09/12；现金 100000；Python3 分钟模式；
佣金双边 0.0003、最低 5 元、卖税 0.0005、零滑点、参与率参数 0.25。
AQ 独立行情仍为已冻结 AD/TDX 输入，SHA256
`8e9320af8ac8b5c51dc5ca67112199c8969fe3a34a71d1a20497d24a9d2c52b2`。

| 策略 | AQ 成交数 | JQ 成交数 | AQ 最终现金/权益 | JQ 最终现金/权益 | AQ − JQ |
|---|---:|---:|---:|---:|---:|
| shared_cash_t1 | 4 | 5 | 99818.055 | 99793.5454 | +24.5096 |
| visible_momentum | 2 | 2 | 100252.0624 | 100184.042 | +68.0204 |

两端最终持仓和挂单均为空。上述为执行行为的两日样例，不是投资绩效结论。
金额核对使用 Decimal，AQ 原生浮点账本不改写；核算容差 `1e-7` 元，原始尾差另存。

### 信号与提交

共享资金案例七次 API 请求的证券、时间、金额/数量、限价及当前价完全一致。
动量案例读取 10 月 9 日 09:46–09:50 共五个标签：

| 股票 | 五个 close | last / first − 1 |
|---|---|---:|
| 600000.XSHG | 9.70, 9.69, 9.68, 9.70, 9.69 | −0.0010309278350515427 |
| 000001.XSHE | 11.71, 11.72, 11.71, 11.72, 11.73 | +0.0017079419299743659 |

两端都在 09:50 选择平安银行并提交 80000 元买单，下一交易日 13:10 提交清仓限价单。
捕获的所有历史标签都不晚于策略当前时钟；历史 close、提交当前价与独立封存的 TDX 条一致。
这证明本窗口的可观测输入一致，不认证 TDX 未知的历史首次可见时刻。

### 成交和资金差额

共享资金：JQ 09:40 同步成交 4600 股浦发（9.68）、3800 股平安（11.73）。
额外 2000 股浦发请求因现金不足被 **缩量到 1100 股并成交**，现金余 218.2694。
AQ 先冻结共享现金、拒绝整笔额外请求，前两单在 09:41 成交，现金余 10871.2694。
两端当天 09:45 的卖出请求均被 T+1 拒绝；次日可卖数量已解锁。
JQ 次日 09:40 卖出 5700/3800 股，AQ 09:41 卖出 4600/3800 股，成交价均为 9.67/11.72。
额外 1100 股在 JQ 产生毛亏 11 元、买佣 5 元、额外卖佣及税 8.5096 元，
合计 **24.5096 元**。不能把“缩量成交”称为本例真实成交量上限导致的部分成交。

动量：JQ 09:50 按当时 last_price 11.73 成交 6800 股；AQ 09:51 按下一条 open 11.72 成交。
JQ 次日 13:10、AQ 13:11 均按 11.77 清仓。相同数量产生 68 元买价差和 0.0204 元佣金差，
合计 **68.0204 元**。TDX 的同标签 close 与下一标签 open 可独立重建这个差额。

JQ Order 的 `commission` 本例含卖税，Trade 无单独 commission/tax 属性；
审计以 Order 合并费用核对 `max(5, turnover*0.0003) + sell_turnover*0.0005`，
不把缺失字段转成零。AQ 原生 Trade 的佣金、税分别核对。

### 估值与生命周期

10 月 9 日 AQ 样例最后选择条是 14:56：浦发 9.56、平安 11.61；
JQ 盘后估值采用 15:00 收盘 9.54/11.59，均与既有 TDX 封存标签相符。
共享资金第一日权益差 327 元包含额外持仓及末价时钟；动量第一日差 204.0204 元
包含买价/佣金差 68.0204 及 6800 股末价差 136 元。
最终双方已清仓，最终差额不受盘后末价影响。

`before_trading_start`、定时回调、`handle_data`、盘后回调都有真实观测。
两端 09:40/09:50/13:10 的业务回调时刻一致，定时回调先于同刻 `handle_data`。
JQ 观测有 09:35/13:00，无 11:30；AQ 研究 profile 有 11:30，无 09:35/13:00 BAR。
12:00 均无分钟回调。边界分钟时钟不能据此宣称完全一致。
JQ `initialize` 时钟为首日 00:00，AQ 为 09:00。
JQ `on_strategy_end` 在最后一天 23:59:59.001 执行；AQ 不支持这个额外生命周期入口，
以原生完成清单、结果及 15:30 快照证明终态。不要把它当作 AQ 已实现的回调。

## 观察脚本如何迁移

`build_scripts.py` 生成四个脚本（两例 × 两平台）。它保留全部业务函数的 AST 决策和
订单参数；测试剥离观察层后逐函数与原文件 AST 比较，原生实跑再比较经济字段。

- JQ 将交易函数/Style 注入全局，不能按 AQ 示例写法从 `jqdata` 具名导入这些交易函数。
  首次真实运行报 ImportError；随后统一为 JQ 常见 `from jqdata import *`，错误证据保留。
- 原示例中的 `assert` 包含 AQ 特有现金冻结/拒单预期。观察版一次求值后记为
  `assertion_observation`，不因 JQ 不同订单规则中断后续原始业务。
  共享资金的 `locked_cash > budget`、额外订单返回 None 在 JQ 为 false，明确保留。
  `record(shared_cash_rejection=1)` 是原示例固定图表值，不能作为真实拒单证据。
- 加入身份、费用配置、回调和账户日志；所有订单/历史调用仍只调用一次原 API。
  AQ 参数由原生配置控制，JQ 额外设置 `order_volume_ratio=0.25`；两端实际容量算法不同。
- 观察器 `_fields` 将未实现属性记成明确 `unavailable`，仅用于诊断，不改变策略判断。
  不能由此声称 AQ 支持 `today_amount`、交易所限价、开盘价或全部 Trade 属性。

因此这是**少量语法调整加显式观察层的真实策略比较**，不是“原脚本零修改完全兼容”。
脚本只用于可信代码；不构成沙箱。

## 离线复验

在仓库根目录、已安装原生依赖的 Python3.12 环境中执行；模块方式保留 `examples` 导入。
`AQ_TDX_EVIDENCE` 指向原始封存数据包，`AQ_JQ_MINUTE_EVIDENCE` 指向本次私有浏览器证据包。
原始行情、网页日志和截图不进入 Git/sdist/wheel。

```sh
python -m examples.jq_minute_comparison.build_scripts --output /new/scripts
python -m examples.jq_minute_comparison.run_aq \
  --input "$AQ_TDX_EVIDENCE/input.json" --output /new/aq-candidate --accept-research
```

第一步生成的 `*.jq.py` 在聚宽编辑器运行，设置上文相同日期、现金及分钟频率。
从免费日志页读取完整 `AQMIN_JSON` 序列，并从“策略代码”回读实际存储源码。
“回测完成”可能早于最终日志刷新；应等到 `complete` 哨兵再封存，不能补写缺尾日志。
最终共享资金 86 条、动量 54 条；网页成交/持仓也单独保存以交叉核对。
这批复用了既有参考策略，源代码已逐字恢复并重载校验；设置已在打开的草稿还原。
聚宽重载会恢复最近一次分钟回测参数，重跑原日频策略前须再次设为
2022-04-01..15、1000000、每天。没有为恢复参数重跑无关日频参考。

已有 AD 0.9.1.dev13 的**独立导出环境**读取封存采集（AQ 原生环境仍使用 AD 0.8.0.dev2）：

```sh
/path/to/export-env/bin/python -m examples.jq_minute_comparison.export_boundary \
  --tdx-root "$AQ_TDX_EVIDENCE/sources/tdx" \
  --ad-wheel "$AQ_TDX_EVIDENCE/dist/ashare_data-0.9.1.dev13-py3-none-any.whl" \
  --output /new/tdx-boundary.json
python -m examples.jq_minute_comparison.compare \
  --input "$AQ_TDX_EVIDENCE/input.json" --aq /new/aq-candidate \
  --jq "$AQ_JQ_MINUTE_EVIDENCE" --boundary /new/tdx-boundary.json \
  --output /new/comparison.json
AQ_TDX_RESEARCH_INPUT="$AQ_TDX_EVIDENCE/input.json" \
AQ_JQ_MINUTE_EVIDENCE="$AQ_JQ_MINUTE_EVIDENCE" python -m pytest tests/minute -q
```

外部证据目录的 `aq-candidate/` 应为同版 `run_aq` 输出；从交付包可直接重验。
没有真实捕获目录时，相关审计测试明确 skip，不用合成输出冒称平台验证。
审计只适用于这两例立即整笔成交的 JQ 订单，遇到其他成交形状直接拒绝。

## 尚未对齐或未验证

- AQ 继续执行原生下一分钟 open 模型；JQ 实测同步 last_price、资金不足可缩量。
- `get_orders/get_trades` 在 JQ 仅返回当日集合，AQ 是本次原生运行集合；
  JQ 已完成订单 status 为 `held`，AQ 为 `filled`，对象属性集合不同。
- 此次没有在 JQ 实测部分成交、午休挂单跨段、日末过期、停牌/涨跌停排队或公司行动。
  AQ 已有原生边界回归不能代替这些 JQ 对照。两个参数同为 0.25 不意味着容量模型相同。
- 原严格三股题、B27 缺条、历史 PIT、真实交易所限价和公司行动完整性仍未通过。
  TDX 未知值和三项研究同意保持；任何 JQ 属性均未回填为 AQ 独立行情事实。
- 原生结果中的 `jq_comparison=NOT_PERFORMED` 仍描述单次本地执行，不被事后外部比较覆写；
  外部 `aq-jq-minute-observation.v1` 报告单独保存本次范围与结论。

官方 API 文档本次经授权浏览器读取：[生命周期及订单规则](https://www.joinquant.com/help/api/help#name:api)。
免费预算在运行前后均显示当日已运行 0 分钟、免费可用 60 分钟（按分钟取整）；
六次尝试合计页面报告 17 秒，其中一次导入失败，五次成功。
未使用付费“导出”、因子查询、充值、会员升级、模拟交易或实盘。
