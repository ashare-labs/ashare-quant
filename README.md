# A宽 · ashare-quant

## 项目简介

A宽是本地 A 股策略、回测与模拟后端。A数达负责数据，RQAlpha 负责原生撮合和账户。支持日频 JSON 规则和可信 Python 策略、多证券组合、分钟研究、持久任务及 paper 模拟；数据覆盖与可用运行模式以本次目录、预检和冻结回执为准。

当前候选版本 `0.12.0.dev20` 使用功能目录和同一份新版 A数达运行时。源码身份随实际文件内容计算，当前候选不继承历史独立验收结论。

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

在配对 quant-web 的正式数据页选择证券、日期和来源，查询后发布回测输入。发布携带预热期、交易日历和来源回执；行情或预热缺失会明确拒绝。缺少公司行动和价限证据的窗口只能在显式确认后运行价格诊断，结果不表示完整经济收益。

固定评测使用独立控制库和明确的便携 owner，不作为正式产品默认数据：

```sh
export ASHARE_QUANT_DAILY_STORE=/path/to/new/data-store
ashare-quant --db .state/tasks/control.sqlite3 task-catalogue
ashare-quant --db .state/tasks/control.sqlite3 task-data-bind --dataset DATASET_ID
ashare-quant --db .state/tasks/control.sqlite3 task-create --request request.json
ashare-quant --db .state/tasks/control.sqlite3 task-worker --once
```

先从 catalogue 获取真实参数范围、数据绑定和假设摘要，再构造请求。缺数据、运行依赖或必要证据时，保留结构化拒绝信息。

## 使用指南

- [Python 策略工作台](docs/python-workbench.md)：自动准备正式数据、运行自定义分钟 Python、查看与取消任务。
- [日频任务与策略](docs/tasks.md)：绑定、参数、可信代码、队列、结果及恢复。
- [数据选择、运行与恢复](docs/data-runtime.md)：资产清单、统一端口、完整备份与空目录恢复。
- [组合和公司行动](docs/portfolio.md)：多证券导入、预检、原生组合和权益到账。
- [分钟与合成回测](docs/backtests.md)：模块入口、示例和边界。
- [paper 模拟](docs/paper.md)：新账户、观察输入、连续状态及恢复身份。
- [数据与诊断](docs/providers.md)、[开发与契约](docs/development.md)、[迁移说明](docs/migration.md)。

统一入口的 API 默认端口为 `18839`、paper 为 `18818`、web 为 `41839`。Python 工作台前缀 `/api/v1/python`，正式数据前缀 `/api/v1/product`，日频评测 `/api/v1/tasks/daily`，组合 `/api/v1/portfolio`，已发布研究行情 `/api/v1/market`。paper 使用独立状态根。生成契约位于 [docs/contracts](docs/contracts)。

## 限制与说明

这是本地可信策略环境，不是任意代码沙箱或实盘券商。历史来源、PIT、分钟完整性、价限与公司行动的未知项必须保留；合成数据不能代表真实市场验收。金额保持十进制字符串，股数和成交由原生账本决定。

新版使用新数据库、新任务和新账户。旧回执、恢复状态、封存源码及历史审阅摘要不改写；既有运行包继续绑定原环境。
