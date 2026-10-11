# 固定策略与连续模拟整合：paper-api.v2

本修订整合 `efc2586` 的原生连续账户与 `77b1fbb/f9b0a60` 的固定日频策略。三者共同祖先为 `b6728f9`。合并的唯一文本冲突是 pyproject 的文档打包清单，已同时保留；策略和 paper 原生执行模块分别保留，没有用前者的全窗重跑替换后者的跨日账本。

## 数据模式与策略收据

`synthetic_fixture` 保留原基础入口。新增 `historical_research` 只允许显式本地导入的 AD 研究导出，必须接受 owner 的 retrospective state model，并核对 `historical_pit=false`、`execution_permission=false`、研究 role、history 查询摘要及全题字段覆盖。该模式不提供实时执行准入。

源文件和题面复制到运行目录并以 SHA256 固定。会话绑定策略纯函数源码、题面、参数、数据和运行时；HTTP 不接受文件路径或策略代码。每日 `paper-strategy-receipt.v1` 包含：策略与题面版本、输入摘要、120 日历史摘要、前收信息截止、前一已提交权益、父检查点、是否调仓、目标、选择结果和来源假设。无调仓时沿用实际持仓，避免重复上日未完成订单。前一权益从原生已提交报告读取，不从另一个 SQL 账户计算。

`Decision` / `CloseInput` 新增 `clock_basis`。研究模式是 `retrospective_model`，原有 `observed_at` / `received_at` 字段在此模式下**明确为模拟可用时刻，不是源接收时间**。实际来源时钟、研究状态假设和覆盖边界仍在不可变 AD 导出及收据来源中。界面显示“非在线新鲜度证明”，不把历史时钟改名为在线实时数据。

公司行动连续应收、股数变更和到账恢复尚未开放。有登记日权益时停在首次可能入账日。新模拟模式允许正常完成登记日；除权/到账区间只有已提交登记日原生账户证明该证券权益为零才可继续，收据保留登记日 checkpoint 和数量。它不把事件改成“无公司行动”。未知权益仍停止。字段缺失、过期、未知规则和完整性冲突仍停止。

## 冻结模拟风控 v1

旧 `conservative_v1` 保持单笔 50,000 CNY、总敞口 80%、现金余量 10%、涨停价预算。其与 1,000,000 本金、98% 固定策略目标不相容：首个真实买单预算为 130,019～358,904 CNY；旧逻辑还累计调仓前持仓与买入目标、预留涨停价现金而未等待实际卖出成交。本修订为独立新会话提供显式 `target_then_native_cash_v1`，不改策略公式、参数或题面，不修改旧会话。

配置位于 `examples/daily-paper-research-risk.json`，在九组全窗运行前冻结：前收目标总市值上限 98%、前收目标单股 34%（容纳固定三股等权目标 98%/3）、双边前收换手 200%（一次全额卖出再买入）、最多8只、禁 ST；新买单上限为 min(1,000,000 CNY, 当前原生 NAV 的40%)，执行后单股上限40%，逐单现金底线为0。40%是声明的模拟开盘跳空余量；98%策略自身保留2%名义现金，不承诺所有跳空后目标均可成交。

前收检查按目标组合和增仓部分计算；持仓价格漂移不阻断减仓或无调仓日。执行按固定日频开盘研究模型走原生市价委托，每笔买入前读取**当时原生可用现金**、冻结订单和实际仓位，费用包含最低佣金；先卖后买，只用已实际成交卖出的收入。不足则拒绝本笔，收据保留证券、测量值、限额、原因，保留此前真实成交。没有信用、预计卖出收入或第二份账本。独立逐笔对账使用高精度 Decimal，现金/NAV误差上限 0.000001 CNY。

## 入口与 API

导入研究包（已有公共 AD 导出由数据任务提供，不在 AQ 内采集）：

```sh
PYTHONPATH=src /path/to/native/python -B -m ashare_quant.paper --root /path/to/store \
  bind-replay --manifest examples/daily_portfolio/manifest.json \
  --input /path/to/w2020-model.json --strategy trend --window w2020 \
  --account review-trend-w2020 --accept-owner-state-model \
  --risk examples/daily-paper-research-risk.json
```

单日推进：`... --root /path/to/store advance --session <id> --day YYYY-MM-DD --expected-parent <上日sha>`；首日省略 parent。每天原生进程只接收一个新日和上日检查点。重复同日请求返回原结果；中断后先 `resume --reason ... --expected-parent ...`，再提交同日。完成检查点已存在但发布失败时仍只发布既有结果。

旧 `/api/v1/paper` 接口继续存在。新增：

| 方法与后缀 | 内容 |
| --- | --- |
| GET `/capabilities` | `paper-api.v2`、固定源码摘要、研究模式与所有待验能力 |
| GET `/schema` | 当前完整 OpenAPI，供前端精确比较 |
| GET `/sessions` | 本地已注册会话与冻结配置 |
| GET `/risk-profiles` | 明确的保守配置和日频模拟 v1 配置 |
| POST `/sessions/{id}/fork` | `{account_id, risk}`，复制当前绑定策略/数据建立从窗口首日开始的独立空账户；不复制或修改原账户状态 |
| GET `/sessions/{id}/view?day=YYYY-MM-DD` | 解码收据、状态、已核验报告和原生账户、来源、错误与下一日；不接受未核验报告 |
| POST `/sessions/{id}/advance` | `{day, expected_parent, reason}`，由固定策略生成目标、封存行情并执行一天 |
| POST `/sessions/{id}/days/{day}/work` | `{expected_parent, reason}`，执行已封存日；外部意图入口复用原 worker |

`prepare` 和 `seal_close` 对绑定策略会重新计算并比较完整冻结输入，不能伪造收据摘要后替换目标。业务错误保留机器码、中文说明、细节和下一步。所有 HTTP 响应 `Cache-Control: no-store`。运行目录含可信本地产生的原生序列化状态，不接收远程 checkpoint。

旧保守门禁复现：`PYTHONPATH=src /path/to/native/python -B scripts/verify_paper_integration.py --output <新目录> [--owner-input-dir <目录>]`。新全窗模拟：`PYTHONPATH=src /path/to/native/python -B scripts/verify_paper_research.py --output <新目录> --owner-input-dir <固定AD导出目录> --reference-dir <固定真实策略baseline目录>`。两者都保留全部八证券、三策略、三窗，不跳过失败日。新矩阵每天检查四个新事件、日结计数、成交ID唯一、信号、成交/费用、可卖持仓、现金及NAV，并验证最终日重复请求的完整报告相等。

实际整合验证：九组中八组完整 485 个账户交易日、278 笔唯一成交，与固定真实策略参考逐日零差异。2022等权前26日13笔一致，2022-04-08因000651.XSHE登记日持有3500股停下，未改已提交账本。动量/趋势登记日零持仓证明允许继续。新增卖出成交后现金释放、跳空资金不足只拒绝本笔、成交中SIGKILL恢复及重复请求测试；全部401 pytest通过、86依赖项跳过。JQ九组参考已由独立任务完成，当前只读其固定交接；2022等权分红金额/税口径另行核对。现有明确历史研究模型不能升级为在线已验。剩余为最终AD执行用途时点/增量契约、有权益公司行动的原生续接与独立人类验收。
