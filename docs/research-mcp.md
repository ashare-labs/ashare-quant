# Agent 研究 MCP（首版）

A宽在现有 HTTP 后端之上提供可选 MCP 薄适配。它复用 AD 已发布数据、AQ 输入冻结、共享异步队列、幂等、原生回测、取消及结果；不建立另一个计算或调度系统。首次使用先调用 `research_capabilities`。

## 可用范围

| 研究目标 | MCP 工具 | 当前行为 |
| --- | --- | --- |
| 确认环境和能力 | `research_capabilities` | 实际查询产品配置、分钟 Python catalogue、AQ OpenAPI；返回环境指纹、产品上限及扩展状态 |
| 发现数据 | `discover_data` | 按查询词分页列出已发布证券和分钟数据源 |
| 覆盖和日行情 | `inspect_data` | 复用 coverage/prices，返回覆盖证据和价格页；覆盖未就绪不会被改成已就绪 |
| 分钟行情 | `read_minute_prices` | 单证券、有界时间窗、分页；保留缺失和数据源标记 |
| 提交自定义策略 | `submit_strategy` | 自定义分钟 Python 源码及 JSON 参数，返回现有 `job_id` 和 queued 状态；不等待回测完成 |
| 找回提交 | `find_research_job` | 按原始幂等键查原后端任务，包括适配进程重启后 |
| 任务跟踪和停止 | `research_job`、`cancel_research_job` | 原后端状态与取消确认，支持分钟 Python、portfolio 和固定日频任务 |
| 日志及结果 | `research_logs`、`research_result` | 摘要默认；分钟任务支持成交、持仓、权益、订单、策略日志分页 |

分钟产品上限仍是 **8 股票、23 交易日、20000 根股票分钟线**（提交日期最多跨 31 日），初始/终结空仓、固定股票池、回溯研究、非历史 PIT；Python 内存不能断点恢复。不能把底层引擎更大的容量描述成当前产品能力。portfolio 和固定日频任务目前提供管理与摘要；其提交仍使用原后端正式工作流。

因子统一接口及新日频 Python 所在 Air 提交尚未在本分支出现。首版发现 `/api/v1/factors/{catalogue,sources,jobs}` 路由并返回后端 OpenAPI 指纹，但 **不会仅凭路由存在启用因子计算**。因子/Alphalens 与新日频 Python 均明确 `available=false`；没有第二套实现，也没有以 mock 宣称真实集成。接入时应对照 AQ `3424fa8`、AD `d787254` 以及日频 AQ `ba24db4` / `0ebe45f` 的实际合并契约，扩展严格请求模型、限定路径与结果投影，再增加真实 worker 验收。现有契约测试覆盖“路由已出现但适配尚未验收”仍不可用的情况。不要复用旧固定日频 API 假装新自定义日频已可用。

## 安装及启动

沿用仓库 Python 3.12.13 和已固定的 AD wheel（参见[安装指南](../README.md#安装指南)）。MCP 依赖是可选 extra，普通 API/worker 不需要导入 MCP SDK。

```sh
uv sync --frozen --extra native --extra mcp
```

先按现有部署清单启动 **同一配置** 的 API 和 task-worker。MCP 不会自行启动或改写服务、初始化数据、采集或发布。假定已运行的研究 API 位于 `http://127.0.0.1:8000`：

```sh
# 本地 Codex / DSH 等 MCP host 的子进程入口
.venv/bin/ashare-quant-mcp --backend-url http://127.0.0.1:8000

# 相同工具逻辑，仅本地 HTTP 调试
.venv/bin/ashare-quant-mcp --backend-url http://127.0.0.1:8000 \
  --transport streamable-http --port 8766
```

HTTP endpoint 是 `http://127.0.0.1:8766/mcp`。CLI 固定绑定 loopback，没有公网 host 参数，SDK 保留 Host/Origin 校验。backend URL 必须是带端口的 `127.0.0.1` 或 `::1` HTTP origin，不允许凭据、路径、查询串或重定向；不继承代理环境变量。stdio stdout 专用于协议，日志写 stderr。Host 中配置以上 command/args，并替换为该 host 可执行的绝对虚拟环境路径；不把 strategy 源码放在命令行。

代码使用官方 Python SDK `mcp==2.3.0`。2026-07-28 协议和 2025-11-25 initialize 握手由 SDK 处理，stdio/Streamable HTTP 运行同一组工具。核对依据：[官方 SDK](https://py.sdk.modelcontextprotocol.io/)、[旧客户端兼容](https://py.sdk.modelcontextprotocol.io/run/legacy-clients/)、[Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)。

## 一次研究迭代

1. 调用 `research_capabilities`，记录产品 `configuration_id` 和分钟 Python `environment_id`；读取当前模型限制。
2. 用 `discover_data` 发现已发布证券，带相同 `configuration_id` 做 `inspect_data` / `read_minute_prices`。coverage 的 ready 指认证覆盖是否满足，不能替代对分钟回溯模型的明确接受。
3. 用户确认信任源码和研究模型后，传 `submission` 对象给 `submit_strategy`。字段与后端 `PythonCreate` 一致：`environment_id`、`name`、`idempotency_key`、`source`、`parameters`、`securities`、`start_date`、`end_date`、`initial_cash`、`trusted_local_code: true`、`accept_research_model: true`。两个确认字段没有默认值，适配器不会自动同意。
4. 保存立即返回的 `job_id`，用 `research_job(family="python_minute", job_id=...)` 跟踪 worker。`research_logs` 获取后端日志；失败状态保留 code/message/source_line 等诊断。
5. 完成后先 `research_result`（默认 summary），再按需要指定 `section` 为 `trades`、`positions`、`equity`、`orders` 或 `strategy_logs`，传回 `next_cursor` 读完整页面。
6. 改参数开始新实验时生成新幂等键，保留相同源码、环境身份及明确确认。切换部署或数据发布后须重新发现和确认环境。

源码由现有受信任本地 Python 执行模型处理，**不是恶意代码隔离沙箱**。MCP 工具不提供任意文件、shell、SQL、实盘、采集、源数据修改或发布入口，但受信任策略本身仍具有后端进程的本地代码权限。必须由 host/用户决定源码是否可信，不能对不受信任多租户开放。

## 重试、分页与容量

提交保留原后端幂等键；并发同键同请求返回同任务，改请求重用键会冲突。提交 HTTP 超时/连接丢失或不确定响应后，适配器仅查询原任务，不自动再次 POST。找到相同冻结请求则返回原 job；否则返回 `SUBMISSION_UNCERTAIN` 及原键。此时查 `find_research_job`，或重试**完全相同请求和键**；“暂未找到”不证明未执行。后端提供持久幂等状态，适配器没有自己的任务库或调度器。

`cancelled` 是最终取消，`cancelling` 是请求已登记但 worker 尚未确认。MCP 协议请求取消不等于后端研究任务取消；显式使用 `cancel_research_job`。Python 任务取消或进程丢失后不能以适配器重放伪装恢复。

单页最多 200 行、行数据最多 64 KiB；单工具返回最多 128 KiB。摘要不包含整份行情或 native 结果。日查询额外收紧到 8 股票/31 日，分钟取数为单股票/3 日。游标绑定查询、分区和内容指纹，结果变化或游标错用会要求从第一页重读，不静默漏行。日志运行中会追加，因此日志游标变化后需重读。持仓页是原生日末账户快照中的持仓，权益页是日末账户字段；不重新计算绩效。

现有后端 result/logs API 尚无服务端分页；适配器每次在 **32 MiB 响应上限** 内读取、校验并投影，只将需要的页面交给 agent。超限明确失败，并提示用现有后端导出；没有引入任意文件资源读取。未来更大的结果应先增加后端有界结果接口，不能取消限制或一次把全部结果塞入上下文。

## 验证与交接

2026-10-22 的 mini 验收记录：全库回归 901 passed / 481 skipped（外部真实数据及显式环境未提供）；最后的 MCP 错误边界调整后，MCP、HTTP 契约和分钟任务定向回归 36 passed。最终实际 wheel 在仓库外独立安装，18 项 MCP 测试全部通过，安装包源码指纹与源码环境相同。两次合成策略（100 股/200 股）各产生 2 笔成交，末值分别为 99998.98 和 100007.96；所有结果页等于直接读取同一后端结果。该数值仅作工程验收，不能解释为实盘收益证据。

最终 wheel SHA-256：`e1e89bc453672837f19b1179d45e3b2ed99fb1cc75f2094bfef58fb1f509d6ea`。

```sh
python scripts/export_mcp.py
python scripts/check_structure.py
ruff check src scripts tests
python scripts/run_tests.py -q tests/mcp
```

[工具契约快照](contracts/research-mcp.tools.json)由真实 SDK `tools/list` 生成。`tests/mcp/fixture.py` 使用已安装 AD `ReleaseWriter` 创建隔离合成 release（1 股票、2 执行交易日、480 分钟），不下载市场数据、不伪装为市场来源验收。测试通过临时 loopback API、官方 stdio/HTTP MCP 客户端及现有 worker/RQAlpha 完整执行。部分原生元数据中的 `real_market_data` 是既有 release 路径标记；该测试明确使用合成数据，该字段不能被引用成真实行情验收。

覆盖协议与 schema、旧版握手、来源/环境/路径边界、摘要和全量分页逐项对照直接 HTTP 返回、参数再测、丢失响应恢复、并发幂等、排队/运行中取消、策略失败、Host/Origin、响应字节上限和可选扩展失效行为。打包验收还要安装实际 wheel，在仓库外运行同样闭环，确认模块从 wheel 环境加载。没有生产部署或 dot 已连接的结论。

远程 dot 需要另行获准的长期宿主、可达 HTTPS MCP 地址、身份认证/授权与 host/origin 白名单、同机可信研究后端和 worker，以及部署管理及数据权限安排。当前本地无认证入口仅供可信本机调试。不得通过临时公网隧道、扩大凭据权限或关闭安全校验“完成连接”；本次未实施这些条件，mini 也不是常驻服务器。
