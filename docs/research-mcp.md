# Agent 研究 MCP

A宽在现有 HTTP 后端之上提供可选 MCP 薄适配。它复用 AD 已发布数据、AQ 输入冻结、共享异步队列、幂等、原生回测、取消及结果；不建立另一个计算或调度系统。首次使用先调用 `research_capabilities`。

## 可用范围

| 研究目标 | MCP 工具 | 当前行为 |
| --- | --- | --- |
| 确认环境和能力 | `research_capabilities` | 实际核对产品配置、Python/因子 catalogue、来源与 AQ OpenAPI 契约；返回环境指纹、上限及扩展状态 |
| 发现数据 | `discover_data` | 按查询词分页列出已发布证券和分钟数据源 |
| 覆盖和日行情 | `inspect_data` | 复用 coverage/prices，返回覆盖证据和价格页；覆盖未就绪不会被改成已就绪 |
| 分钟行情 | `read_minute_prices` | 单证券、有界时间窗、分页；保留缺失和数据源标记 |
| 提交自定义策略 | `submit_strategy` | 自定义分钟/日频 Python 源码及 JSON 参数，返回现有 `job_id` 和 queued 状态；不等待回测完成 |
| 因子目录 | `discover_factors` | 按库、名称查询并分页返回真实定义、依赖、历史需求；保留 Alpha191 和 Qlib Alpha158 命名空间 |
| 因子来源 | `factor_sources` | 登记来源的就绪状态、口径、边界；指定 source_id 后分页列出证券 |
| 因子计算与评价 | `submit_factor` | 复用 FactorCreate、冻结输入、既有 worker 和 AD Alphalens 评价，可选 label_end |
| 找回提交 | `find_research_job` | 按原始幂等键查原后端任务，包括适配进程重启后 |
| 任务跟踪和停止 | `research_job`、`cancel_research_job` | Python/因子状态与取消；portfolio 和旧固定日频仅只读状态 |
| 日志及结果 | `research_logs`、`research_result` | 摘要默认；Python 支持成交/持仓/权益/订单/日志；因子支持值、评价摘要及全部诊断分页 |

Python任务使用冻结发布，策略通过查询接口提出数据需求，通过set_universe声明交易订阅；不要求外部固定证券池，也没有独立证券数或日历跨度上限。数据可见性、实际覆盖和当前查询/任务资源预算仍需满足。Python内存不能断点恢复。portfolio和旧固定日频仅通过历史接口提供查找与摘要；其创建和执行平台已退役，取消明确拒绝。

适配版本 `research-mcp.v1.1` 保留原 10 个工具名，新增 3 个因子工具；原分钟提交默认仍为 `1m`。
适配器与当前AQ安装包共用请求模型，AD版本和来源身份以安装包的固定依赖及运行时验证为准。
能力发现同时检查实际 schema 和 live catalogue/sources；只有路径、HTTP 200、旧日频固定任务均不能启用扩展。
`available` 表示接口与运行模块可用；具体来源的可计算字段、历史上下文和执行参考状态仍由来源及准备结果判定。

日频指定 `frequency="1d"`，分钟指定 `frequency="1m"`；兼容字段`warmup_sessions`不替代实际历史查询需求。
末持仓按最后收盘标记。`python_daily` 与原 `python_minute` family 是同一后端 `/python` 队列的路由别名，
任务详情中的 frequency 才是实际频率。`daily` 仍指只读历史日频任务，不能与自定义 Python 混用。

因子目录共 368 项：19 项聚宽命名研究因子、191 项 Alpha191、158 项 Qlib 工程特征。
例如 `alpha191:alpha158` 不是 `qlib_alpha158:ROC5`。不把特征列数当作有效策略数。
`factor_sources` 只接收登记 ID；目录证券页沿用后端可浏览范围，来源若另有更大的 total_securities 会明确保留。
因子不设独立证券数或日历跨度上限；按实际输入量保留100000个证券交易日和32 MiB输入预算，计算语义由后端负责。
计算与评价在同一任务完成；`evaluation` 可选，收益价格必须有其声明的复权证据和标签缓冲。
CMES 的 VWAP 是已观测同一行 amount/share-volume，零量保持缺失；不宣称供应商 VWAP 或历史 PIT 认证。

## 安装及启动

沿用仓库 Python 3.12.13 和已固定的 AD wheel（参见[安装指南](../README.md#安装指南)）。MCP 依赖是可选 extra，普通 API/worker 不需要导入 MCP SDK。

```sh
uv sync --locked --extra native --extra factor-evaluation --extra mcp
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
4. 日频显式传 `frequency="1d"` 和 `warmup_sessions`，仍须用户两项确认。保存立即返回的 `job_id`，用 `research_job(family="python_minute" 或 "python_daily", job_id=...)` 跟踪 worker。`research_logs` 获取后端日志；失败状态保留 code/message/source_line 等诊断。
5. 完成后先 `research_result`（默认 summary），再按需要指定 `section` 为 `trades`、`positions`、`equity`、`orders` 或 `strategy_logs`，传回 `next_cursor` 读完整页面。
6. 改参数开始新实验时生成新幂等键，保留相同源码、环境身份及明确确认。切换部署或数据发布后须重新发现和确认环境。

源码由现有受信任本地 Python 执行模型处理，**不是恶意代码隔离沙箱**。MCP 工具不提供任意文件、shell、SQL、实盘、采集、源数据修改或发布入口，但受信任策略本身仍具有后端进程的本地代码权限。必须由 host/用户决定源码是否可信，不能对不受信任多租户开放。

## 重试、分页与容量

提交保留原后端幂等键；并发同键同请求返回同任务，改请求重用键会冲突。提交 HTTP 超时/连接丢失或不确定响应后，适配器仅查询原任务，不自动再次 POST。找到相同冻结请求则返回原 job；否则返回 `SUBMISSION_UNCERTAIN` 及原键。此时查 `find_research_job`，或重试**完全相同请求和键**；“暂未找到”不证明未执行。后端提供持久幂等状态，适配器没有自己的任务库或调度器。

`cancelled` 是最终取消，`cancelling` 是请求已登记但 worker 尚未确认。MCP 协议请求取消不等于后端研究任务取消；显式使用 `cancel_research_job`。Python 任务取消或进程丢失后不能以适配器重放伪装恢复。

单页最多 200 行、行数据最多 64 KiB；单工具返回最多 128 KiB。摘要不包含整份行情或 native 结果。日查询额外收紧到 8 股票/31 日，分钟取数为单股票/3 日。游标绑定查询、分区和内容指纹，结果变化或游标错用会要求从第一页重读，不静默漏行。日志运行中会追加，因此日志游标变化后需重读。持仓页是原生日末账户快照中的持仓，权益页是日末账户字段；不重新计算绩效。

现有后端 result/logs API 尚无服务端分页；适配器每次在 **32 MiB 响应上限** 内读取、校验并投影，只将需要的页面交给 agent。超限明确失败，并提示用现有后端导出；没有引入任意文件资源读取。未来更大的结果应先增加后端有界结果接口，不能取消限制或一次把全部结果塞入上下文。

## 因子闭环与结果

1. 从 capabilities 的 `extensions.factors` 取 environment_id；确认 evaluation_available。
2. `discover_factors` 选择准确 ID/definition，再用 `factor_sources` 查看来源口径及证券。
3. `submit_factor` 使用后端 FactorCreate 字段：environment_id、幂等键、名称、factor_id、definition、source_id、securities、history_start、start_date、end_date、parameters、rank_policy，可选 evaluation 和 label_end。
4. 沿用 `family="factors"` 的任务/取消/日志工具。结果默认摘要；`factor_values` 读取完整值、状态、缺失原因；`evaluation_summary`、`ic`、`rank_ic`、`quantile_returns`、`quantile_membership`、`spread`、`stability`、`turnover` 和 `cost_sensitivity` 是直接的 AD 行数据。
5. `evaluation` 分区将完整评价对象投影为 JSON Pointer/value 叶节点，保留空容器、覆盖、警告与口径，便于有界无损读取。摘要中的紧凑元数据可能带截断标志；要完整诊断请分页读取该分区。没有计算新绩效。

## 验证与交接

```sh
python scripts/export_mcp.py
python scripts/generate_contracts.py
python scripts/check_structure.py
ruff check src scripts tests
python scripts/run_tests.py -q tests/mcp tests/factors tests/minute_tasks
```

[工具契约快照](contracts/research-mcp.tools.json)由真实 SDK tools/list 生成。
`tests/mcp/fixture.py` 是明确标记的合成发布；测试通过临时 loopback HTTP、stdio、共享 worker 与实际原生引擎验证，
不将合成行情称为真实市场证据。新增因子测试保留原分钟测试，覆盖目录/来源、Alpha191/Qlib 计算、评价所有分区、
输入冻结、幂等和取消；日频测试检查必填确认、历史查询、真实原生执行和末持仓语义。

`scripts/validate_research_mcp.py --plan PLAN.json --output NEW_DIRECTORY` 接受操作者指定的已有真实发布和来源，
通过临时两种传输执行因子与可选日频请求，与同一后端结果页、独立直调任务、刷新恢复逐项对照。
计划内 Python 请求的两个确认必须由用户明确给出，脚本不会补上；没有 Python 请求时只验因子。
输出仅保留一份回执与原任务状态/冻结产物，源数据不复制、不采集、不修改。结束时关闭临时监听。
实际验收结果和包身份见本次独立候选交接，历史 mini 的计数不作为本次 Air 证明。

打包验证使用 `scripts/verify_wheel.py --extra mcp --extra factor-evaluation --test tests/mcp --test tests/factors --test tests/minute_tasks`，
在仓库外安装实际 wheel 并检查源码身份、资源和动态入口；需要使用显式 wheel 参数和新 output 目录。

远程 dot 仍需要另行批准的长期宿主、可达 HTTPS MCP 地址、认证/授权、Host/Origin 白名单、
同机可信后端/worker 和明确的数据权限。当前无认证 loopback 入口只供可信本机调试。
本次不安装常驻服务，不开公网/隧道，不配置凭据，不改变 VPN/权限，也不宣称 dot 已连接。

`daily`/`portfolio` 家族的查找、状态、日志与结果通过历史接口读取，取消返回 TASK_FAMILY_RETIRED；没有提交固定算法的工具。其余 10 项研究工具与 3 项因子工具继续使用原 HTTP 执行链路。
