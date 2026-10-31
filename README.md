# A宽 · ashare-quant

## 项目简介

A宽（`ashare-quant`）是本地 A 股策略研究、回测与模拟账本后端。系统与 A数达（`ashare-data`）及前端 `quant-web` 配对运行：A数达负责不可变行情、参考与财务数据供给，RQAlpha 提供底层撮合与原生账户账本，A宽提供自定义 Python 策略工作台（`aquant`）、因子计算与评价、共享异步任务队列、收益归因分析、历史模拟账本读取以及 Agent Research MCP 接口。

当前包版本以 [`pyproject.toml`](pyproject.toml) 为准，配对的 A数达 wheel 与源码指纹记录在 [`src/ashare_quant/providers/data-runtime.json`](src/ashare_quant/providers/data-runtime.json)。

## 安装指南

运行环境要求 Python 3.12 与 `uv`。安装前先从配对的 `ashare-data` 源码构建本地依赖 wheel，再按 `uv.lock` 同步锁定环境：

```sh
python scripts/stage_owner.py --source /path/to/ashare-data
uv sync --locked --extra native --extra factor-evaluation --extra mcp
```

依赖锁定、运行时快照（`runtime-lock.json` / `native-runtime-lock.json`）、离线导出与软件许可说明见 [开发、依赖与许可](docs/development.md)。

## 快速开始

所有服务共享同一份 `ashare-deployment.v1` 部署清单（配置格式见 [数据运行与恢复](docs/data-runtime.md)）：

```sh
export ASHARE_DEPLOYMENT=/path/to/data/deployment.json
python -m ashare_quant.deployment doctor
python -m ashare_quant.deployment init

# 启动研究 API、异步任务 worker 与只读模拟账户服务
python -m ashare_quant.deployment api
python -m ashare_quant.deployment task-worker
python -m ashare_quant.deployment paper
```

正式本机部署（`ashare-local`）默认端口为：API `18839`、Paper `18818`、Web `41839`。运行 `python -m ashare_quant.deployment commands` 可打印供 `quant-web` 启动使用的环境变量。

## 使用指南

文档索引见 [docs/README.md](docs/README.md)，按功能域划分为 8 篇指南：

- [数据、启动与恢复](docs/data-runtime.md)：`deployment.json` 资产配置、服务启动、状态与源资产备份恢复、`/api/v1/product` 行情/财务/分钟查询。
- [Python 策略工作台](docs/python-workbench.md)：`aquant` 策略 API、单文件与多文件策略项目、`X-Quant-Workspace` 工作空间、回测归属列表及旧 `jqdata` 导入迁移。
- [回测引擎与异步任务](docs/backtests.md)：分钟与日频回测模型、来源行级准入、共享异步任务队列、流式输入与存储预算。
- [因子看板与离线评价](docs/factors.md)：368 项统一因子目录（聚宽 19 + Alpha191 + Qlib Alpha158）、数据源登记与 Alphalens 评价。
- [结果分析与收益归因](docs/result-analysis.md)：`returns-v1` / `returns-v2` 相对净值超额口径、20 项核心指标定义、比较基准切换与交易盈亏归因。
- [账户历史与运行时迁移](docs/paper.md)：`/api/v1/paper` 只读历史查询、已结算 v3 账本运行时升级授权与持仓续接。
- [Agent 研究 MCP](docs/research-mcp.md)：面向 AI Agent 的 13 个研究与因子工具（`stdio` / `streamable-http`）、分页与幂等提交协议。
- [开发、依赖与许可](docs/development.md)：依赖锁定、运行时快照、OpenAPI/MCP 契约生成、wheel 隔离验证、软件许可与历史接口退役对照表。

## 限制与说明

以下全局边界适用于本仓库所有模块，各分册不再重复赘述：

1. **受信任本地执行环境**：自定义 Python 策略在本地 worker 子进程中以当前用户权限执行，并非面向不受信任多租户的恶意代码隔离沙箱；提交任务需显式确认 `trusted_local_code=True` 与 `accept_research_model=True`。
2. **数据与证据继承**：A宽不自行抓取、修补或伪造行情数据；知识时点（PIT）、停牌、涨跌停限价（如 5%/10% 前收研究模型）与公司行动的覆盖范围均继承自所绑定 A数达发布的回执声明。
3. **代码包与运行产物分离**：源码树与发行 wheel 不包含真实行情库、个人凭据、模拟账户账本或历史回测产物；冻结任务输入与已发布结果保持只读，不随源码升级而静默重写。
