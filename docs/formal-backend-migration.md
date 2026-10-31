# 正式研究后端与前端迁移

> 历史切换记录。路由删除表仍可用来核对已退役接口。现行本机入口是 ashare-local（API `18839`、Web `41839`）与 quant-web 的配对部署，见 [文档索引](README.md)。文末「切换顺序」只保留当时的改造步骤。

当前研究入口使用可信自定义 Python，固定七策略、固定日频窗口和演练运行入口已退役。后端不登记预设算法，也不随 wheel 安装示例源文件、演练行情或演练执行器。开发回归所需旧算法与合成数据位于 `tests/support`、`tests/fixtures`；产品代码不导入它们。

本页记录迁移当时的接口对照和已退役入口，不作为某台机器的部署状态记录。多文件策略与冻结身份见[策略项目](strategy-projects.md)，账号边缘选择的私人文件空间见[单文件与工作空间契约](strategy-files.md)，收益版本与冻结基准派生见[结果分析](result-analysis.md)。安装版本和实际服务身份需在部署时核对。策略运行以 [Python 工作台](python-workbench.md) 为准。

## 历史目录演变

AQ `2aa7829` 与 AD `2fe79d9` 的共同整理确立了功能目录、`snake_case` Python 命名、`kebab-case` 文档、五章 README、真实数据不入发布树，以及源码和依赖变更产生新身份的原则（详见[贡献指南](../CONTRIBUTING.md)）。早期 `m2`、`daily_portfolio`、`rqprobe` 的职责分别进入 `backtests`/`tasks`、`portfolio` 和 `execution`；`m2job-` 等序列化业务标识仍保留原含义，不机械改写旧回执、数据库记录或检查点。策略导入使用 `aquant`，旧 `jqdata` 等别名的离线转换见[导入迁移](aquant-migration.md)。当前 HTTP 契约为 api/tasks/paper 三份 [OpenAPI](contracts) 及研究 MCP 工具契约，生成流程见[开发指南](development.md)。

## 已移除的接口与入口

主 API 删除 36 个 method/path，paper 删除 9 个写接口，另删除 2 个专用 schema 路由。下列接口从路由表和 OpenAPI 实际删除，返回 404（共享路径仅保留 GET 时，旧 POST 返回 405），不会代理到另一套固定算法执行器。

| 原接口/命令 | 变更 | 前端处理 |
| --- | --- | --- |
| `/api/v1/portfolio/**` 全部 15 个路由 | catalogue、策略注册/列举/详情、preflight、service、jobs 创建/读取/控制/结果和专用 schema 全部删除 | 新研究使用 `/python`；旧结果转 `/history` |
| `/api/v1/tasks/daily/**` 全部 17 个路由 | 固定窗口目录、规则/旧 Python 策略注册与目录、数据绑定、任务创建/读取/取消恢复、service、schema 全部删除 | 统一使用自定义 Python 提交，不能继续发送 two_day/w1/w2 等窗口 |
| `GET /api/v1/scenarios`、`GET /api/v1/rqalpha/scenarios` | 演练场景目录删除 | 删除场景选择/演练入口 |
| `POST /api/v1/strategies`、`POST /api/v1/runs`、`POST /api/v1/runs/{id}/cancel` | 合成策略/执行演练创建与控制删除 | 旧 runs 仅作历史读取 |
| `POST /api/v1/data/admission` | 固定合成行情注入删除 | 使用真实已发布数据和覆盖查询 |
| `POST /api/v1/paper/sessions`；sessions 下 fork、ingest、advance、days/{day}/work、decisions、closes、pause、resume | 固定研究会话创建、注入、推进和演练控制全部删除 | paper 只提供已存在会话/账本/报告的只读查看 |
| CLI demo、scenarios、strategy-create、run-create、旧 worker、task-create、portfolio、旧 data 导入/绑定/注册命令 | 专用执行入口删除 | `python-create` 与共享 `task-worker`；历史用 `history` |
| `python -m ashare_quant.portfolio`、旧固定 daily/synthetic runner | 不再安装 | 不允许后台绕开 HTTP 恢复旧平台 |

固定预设 registry、固定窗口 provider、演练数据 admission 和 synthetic/RQAlpha 演练模块均移出产品 src。共享序列化、子进程监督、取消监视、数据契约已提取为通用模块，Python/因子运行不再继承固定任务服务。

## 通用接口与前端新契约

| 用途 | 接口 | 说明 |
| --- | --- | --- |
| 配置、目录和数据 | `/api/v1/product/config`、`catalogue`、`prices`、`coverage`、`minute/sources`、`minute/prices`、`financial/point`、`financial/coverage` | 保持真实发布、来源、覆盖与缺失语义；catalogue 不返回固定算法 |
| 正式数据发布 | `POST /api/v1/product/releases` | 保留通用发布流程及校验；已与 portfolio task service 解耦 |
| 通用行情 | `/api/v1/market/stores`、`/market/{alias}/{dataset}` 及 prices/coverage | 保留只读查询；高级工具的“固定样本行情”只是旧页面名称，不代表应删除数据能力 |
| 环境/频率/运行上限 | `GET /api/v1/python/catalogue` | 只有能力与限制，无 example、预设源码或参数；`frequencies` 为 1m、1d |
| 本机策略文件 | `GET/POST /api/v1/python/directories`、`GET/PUT /api/v1/python/files` | 固定正式 state 根下目录和 UTF-8 `.py` 读写；[接口契约](strategy-files.md)，保存不执行代码 |
| 新研究提交 | `POST /api/v1/python/jobs` | 显式 source 或完整 project、frequency、日期、initial_cash、environment_id、幂等键及两项本机研究确认；securities 可省略，parameters 可选 |
| 日频 | 提交 `frequency=1d`，证券和历史由策略 API 提出 | 按需读取固定发布；无外部证券池/预热要求，使用统一可配置资源预算；末持仓按最后可见收盘标记 |
| 分钟 | 上述提交 `frequency=1m` | 同一按需查询接口与统一预算，支持动态订阅和期末持仓；保留已声明的分钟回溯撮合模型 |
| 状态、冻结输入、日志、结果 | `GET /api/v1/python/jobs`、`/{id}`、`/{id}/manifest`、`/{id}/logs`、`/{id}/result` | 原异步队列、输入冻结、幂等找回与持久结果保留 |
| 取消 | `POST /api/v1/python/jobs/{id}/cancel` | cancelling 是待 worker 确认；cancelled 才是最终取消 |
| 恢复语义 | `POST /api/v1/python/jobs/{id}/resume` | 当前任意 Python 内存不可断点恢复，明确返回 PYTHON_RESTART_REQUIRED；can_resume=false。刷新/进程重开仍能按 job_id/幂等键找回状态和结果，不自动重放源码 |
| 因子 | `/api/v1/factors/catalogue`、`sources`、`jobs` 与详情/日志/取消/结果 | Alpha191、Qlib Alpha158 等实际计算评价和共享 worker 保留 |
| 服务与契约 | `GET /api/v1/tasks/service`、`/tasks/openapi.json` | 取代两个固定任务服务/schema；以实际 OpenAPI 为准 |

资源限制不等于固定验收样本窗口：请求可以选择数据覆盖内的日期、证券和用户代码，后端按真实可用输入检查并冻结。不能由前端填入内置示例策略作为新建默认值。

## 高级工具逐项分类

| 旧前端入口 | 核对到的后端依赖 | 分类与处理 |
| --- | --- | --- |
| `#/tasks` 固定窗口任务 | `/tasks/daily/**` | 固定场景目录、数据绑定与运行入口删除；历史转 `/history` |
| `#/execution/runs` 内部执行演练 | scenarios、strategies、runs、data/admission | 删除目录、注入、创建和取消；历史 runs/strategies GET 保留，空库没有自动种子 |
| `#/paper` 连续模拟 | `/paper/**` | 删除公开会话创建/注入/推进/决策/工作/暂停恢复；GET 保留原账户会话与报告。能力返回 read_only=true |
| `#/data` 数据准备与诊断 | `/product/releases`、product/market coverage、`/diagnostics/**`，以及旧 admission/绑定 | 删除旧 admission/固定绑定；真实发布、覆盖、日历、preflight 诊断保留，不因菜单名删除 |
| `#/market` 固定样本行情 | `/market/**` | 保留通用来源与行情查询；前端去掉固定样本展示假设 |

边界说明：`portfolio` 目录内仍有原生共享现金、账户、信号计算等底层实现，独立 paper/online/intraday 已绑定真实账户的后续处理仍依赖它们；本次不删除、重置真实账户或账本，也没有把这些能力重新暴露为七策略运行平台。相关已有账户 CLI 需保持明确会话绑定。它们未来产品化的交互不在本次前端工作范围。因子 catalogue 是正式研究定义目录，必要诊断不是演练入口，均保留。

## 历史结果与升级隔离

- `GET /api/v1/history/jobs?family=daily|portfolio|python|factors` 返回只读记录；`/{id}`、`/{id}/manifest`、`/{id}/logs`、`/{id}/result` 读取原冻结证据。历史 result href 投影为新路径，数据库原记录不改写。
- `GET /api/v1/history/strategies` 返回旧 registered_strategies 的原始冻结记录和 source，不删除用户策略；原 `/strategies`、`/runs` 的只读接口继续读取既有执行记录。
- 历史读取接受已知 manifest schema，验证接受时 manifest/request/session 摘要与原文件。旧日频/portfolio 结果还验证原 publication seal、文件哈希和长度。Python/因子结果验证原 published.json 摘要与任务身份。失败不会以重新运行或重新计算补齐。
- 返回 `read_only=true`、`can_resume=false`、`verification=frozen_integrity_only` 与原 service identity，不能将完整性校验展示成新经济正确性认证。跨版本历史可读不等于允许跨版本执行。
- 新共享 worker 只执行当前源码身份的 Python/因子任务。旧待处理任务保持原始 stored_status；历史视图 status/display_status 明确为 retired（家族已退役）或 runtime_unavailable（旧源码），附 execution_state 和 execution_reason。Python/因子类型化接口保留原 status，同时返回同样的 display_status 和原因，前端必须优先展示这些字段。旧记录从当前候选队列和容量中排除，service.historical_counts 单列数量，不抢占、不自动恢复、不执行旧算法；明确指定旧任务时报错。未知旧数据库/schema 仍拒绝加载并要求单独审查升级。
- 本次只变更代码和开发夹具，没有真实数据库清理、结果迁移、账户重置或原始数据修改。

## MCP 与切换顺序

保留 10 项研究工具和 3 项因子工具。`daily`、`portfolio` family 仅用于历史查找/状态/日志/结果，取消明确拒绝；自定义日频使用 `python_daily`，不能继续将 `daily` 当新提交入口。能力发现标明只读家族；submit_strategy/submit_factor 仍走原 HTTP、冻结输入、共享 worker 和原生计算。

下列步骤记录迁移当时的前端改造顺序。现行部署以 [文档索引](README.md) 的 ashare-local（API `18839`、Web `41839`）与 quant-web 配对为准。

1. 前端移除七模板和演练菜单/按钮/自动种子；新建策略由用户提供源码与参数，接入 PythonCreate 并显示真实频率限制与确认项。
2. 新状态页使用 Python/因子详情、日志、结果、取消和共享 service；恢复按钮遵守 can_resume，不能把重新提交描述成断点恢复。
3. 旧记录页切 `/history`，按只读标记禁用所有运行按钮；paper/runs 留作历史查看。数据工具接真实发布/查询/诊断。
4. 前后端联合验收后，另行授权停止/备份/切换同配置 API、worker、MCP。当时替换的是仍调用已退役接口的页面；现行界面是同一清单上的 quant-web。

完整当前契约见 [HTTP OpenAPI](contracts/api.openapi.json)、[任务 OpenAPI](contracts/tasks.openapi.json)、[MCP 工具](contracts/research-mcp.tools.json)。
