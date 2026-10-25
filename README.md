# A宽 · ashare-quant

## 项目简介

A宽是本地 A 股策略、回测与模拟后端。A数达负责数据，RQAlpha 负责原生撮合和账户。支持可信自定义 Python 日频/分钟策略、因子计算评价、持久异步任务与历史结果读取；数据覆盖与可用运行模式以本次目录、预检和冻结回执为准。

包版本以 [pyproject.toml](pyproject.toml) 为准，A数达依赖以[运行时 pin](src/ashare_quant/providers/data-runtime.json)为准。源码身份随实际文件内容计算，新源码不继承历史独立验收结论。

## 安装指南

需要 Python 3.12。原生引擎安装前阅读 [许可证](docs/licensing.md)。从明确的 A数达候选构建并核对本地依赖 wheel：

```sh
python scripts/stage_owner.py --source /path/to/ashare-data
uv sync --locked --extra native
```

`uv.lock` 是安装版本和包哈希的唯一锁定来源；`--locked` 会拒绝与项目声明不一致的锁。核心、原生、离线安装及按需 pip 导出见 [依赖指南](docs/dependencies.md)，更新流程见 [开发指南](docs/development.md)。行情、凭据、账户和历史运行产物不随发行包发布。

## 快速开始

使用已有正式日线库时，先按 [数据选择、运行与恢复](docs/data-runtime.md) 准备资产清单。清单引用现有数据，运行状态另存；无需复制整库。所有命令使用同一清单，与当前工作目录无关：

```sh
export ASHARE_DEPLOYMENT=/path/to/data/deployment.json
python -m ashare_quant.deployment doctor
python -m ashare_quant.deployment init
python -m ashare_quant.deployment api
# 另一个终端，使用同一 ASHARE_DEPLOYMENT
python -m ashare_quant.deployment task-worker
```

通过通用数据接口查询已发布行情及覆盖，Python 任务按提交范围准备并冻结输入、日历和来源回执；缺少执行证据会明确拒绝。前端须完成本次接口迁移后再配对部署。

使用 `/api/v1/python/catalogue` 获取环境身份，再提交用户自己的 Python 源码、频率、证券、日期、参数和确认项。接口不返回默认策略或填充策略参数。详见 [前端迁移](docs/formal-backend-migration.md)。

## 使用指南

- [因子看板与评价](docs/factors.md)：已有两来源库、冻结输入、真实数值与同页统计诊断。
- [aquant 策略入口迁移](docs/aquant-migration.md)：新导入、安装边界与可恢复的保存策略迁移。
- [Python 策略工作台](docs/python-workbench.md)：自动准备正式数据、运行自定义日频/分钟 Python、查看与取消任务。
- [研究任务](docs/tasks.md)：共享队列、取消、结果和重连找回。
- [数据选择、运行与恢复](docs/data-runtime.md)：资产清单、统一端口、完整备份与空目录恢复。
- [历史组合与数据发布](docs/portfolio.md)：历史读取及保留的数据能力。
- [通用分钟回测](docs/backtests.md)：模块入口和边界。
- [账户历史](docs/paper.md)：只读历史与独立账户运行时边界。
- [数据与诊断](docs/providers.md)、[开发与契约](docs/development.md)、[迁移说明](docs/migration.md)。
- [结果分析](docs/result-analysis.md)、[财务质量查询](docs/financial-quality-api.md)、[分钟数据发布](docs/product-minute.md)。

统一入口的 API 默认端口为 `18839`、paper 为 `18818`、web 为 `41839`。Python 工作台前缀 `/api/v1/python`，正式数据前缀 `/api/v1/product`，共享任务状态 `/api/v1/tasks/service`，历史任务 `/api/v1/history`，已发布研究行情 `/api/v1/market`。paper 使用独立状态根。生成契约位于 [docs/contracts](docs/contracts)。

## 限制与说明

这是本地可信策略环境，不是任意代码沙箱或实盘券商。历史来源、PIT、分钟完整性、价限与公司行动的未知项必须保留；合成数据不能代表真实市场验收。金额保持十进制字符串，股数和成交由原生账本决定。

已有 v4 控制库中的受支持冻结任务可校验读取；执行仍逐任务绑定源码与环境。旧表结构不自动迁移。旧回执、策略源码、账户和原始行情不删除、不改写。当前 Web 尚未迁移，禁止单独部署此后端候选。

Agent 研究入口见 [Research MCP](docs/research-mcp.md)，复用同一研究 API 和队列。
