# 固定策略与连续模拟整合：paper-api.v2

本修订整合 `efc2586` 的原生连续账户与 `77b1fbb/f9b0a60` 的固定日频策略。三者共同祖先为 `b6728f9`。合并的唯一文本冲突是 pyproject 的文档打包清单，已同时保留；策略和 paper 原生执行模块分别保留，没有用前者的全窗重跑替换后者的跨日账本。

## 数据模式与策略收据

`synthetic_fixture` 保留原基础入口。新增 `historical_research` 只允许显式本地导入的 AD 研究导出，必须接受 owner 的 retrospective state model，并核对 `historical_pit=false`、`execution_permission=false`、研究 role、history 查询摘要及全题字段覆盖。该模式不提供实时执行准入。

源文件和题面复制到运行目录并以 SHA256 固定。会话绑定策略纯函数源码、题面、参数、数据和运行时；HTTP 不接受文件路径或策略代码。每日 `paper-strategy-receipt.v1` 包含：策略与题面版本、输入摘要、120 日历史摘要、前收信息截止、前一已提交权益、父检查点、是否调仓、目标、选择结果和来源假设。无调仓时沿用实际持仓，避免重复上日未完成订单。前一权益从原生已提交报告读取，不从另一个 SQL 账户计算。

`Decision` / `CloseInput` 新增 `clock_basis`。研究模式是 `retrospective_model`，原有 `observed_at` / `received_at` 字段在此模式下**明确为模拟可用时刻，不是源接收时间**。实际来源时钟、研究状态假设和覆盖边界仍在不可变 AD 导出及收据来源中。界面显示“非在线新鲜度证明”，不把历史时钟改名为在线实时数据。

公司行动连续应收、股数变更和到账恢复尚未开放：所有相关登记/除权/到账区间停止，即使当前没有持仓也不能绕过。字段缺失、停牌、过期、未知规则和完整性冲突仍停止。保守默认风控保持不变（单笔上限 50,000 CNY、总敞口 80%、现金余量 10%、按涨停上限和调仓前持仓预算，不预支卖出收入）。固定真实题面 98% 目标与此政策尚不相容，必须等待明确冻结的共同风控协议；不能在同一会话中改政策续跑。

## 入口与 API

导入研究包（已有公共 AD 导出由数据任务提供，不在 AQ 内采集）：

```sh
PYTHONPATH=src /path/to/native/python -B -m ashare_quant.paper --root /path/to/store \
  bind-replay --manifest examples/daily_portfolio/manifest.json \
  --input /path/to/w2020-model.json --strategy trend --window w2020 \
  --account review-trend-w2020 --accept-owner-state-model
```

单日推进：`... --root /path/to/store advance --session <id> --day YYYY-MM-DD --expected-parent <上日sha>`；首日省略 parent。每天原生进程只接收一个新日和上日检查点。重复同日请求返回原结果；中断后先 `resume --reason ... --expected-parent ...`，再提交同日。完成检查点已存在但发布失败时仍只发布既有结果。

旧 `/api/v1/paper` 接口继续存在。新增：

| 方法与后缀 | 内容 |
| --- | --- |
| GET `/capabilities` | `paper-api.v2`、固定源码摘要、研究模式与所有待验能力 |
| GET `/schema` | 当前完整 OpenAPI，供前端精确比较 |
| GET `/sessions` | 本地已注册会话与冻结配置 |
| GET `/sessions/{id}/view?day=YYYY-MM-DD` | 解码收据、状态、已核验报告和原生账户、来源、错误与下一日；不接受未核验报告 |
| POST `/sessions/{id}/advance` | `{day, expected_parent, reason}`，由固定策略生成目标、封存行情并执行一天 |
| POST `/sessions/{id}/days/{day}/work` | `{expected_parent, reason}`，执行已封存日；外部意图入口复用原 worker |

`prepare` 和 `seal_close` 对绑定策略会重新计算并比较完整冻结输入，不能伪造收据摘要后替换目标。业务错误保留机器码、中文说明、细节和下一步。所有 HTTP 响应 `Cache-Control: no-store`。运行目录含可信本地产生的原生序列化状态，不接收远程 checkpoint。

复现：`PYTHONPATH=src /path/to/native/python -B scripts/verify_paper_integration.py --output <新目录> [--owner-input-dir <目录>]`。合成演练单独明示 10,000 CNY 及其测试风险参数；研究矩阵保持全部八证券、三策略、三窗和原风险参数，遇到阻断保留原日与失败，不跳到其他日期。

实际整合验证：三策略合成各三日；真实 SIGKILL 与 UI 恢复；风险拒绝前后检查点/账本不变；同槽重复与修改冲突；信号当前/未来价格隔离；错误字段、行动时点、断网与服务身份阻断。真实研究矩阵仅推进至明确门禁，未通过真实连续模拟。最终 AD 执行数据契约、JQ 修复交接、行动连续恢复、风控协议与独立 quant-web 人类验收仍是待办。
