# A宽 · ashare-quant

## 项目简介

A宽是本地 A 股策略、回测与模拟后端。A数达负责数据，RQAlpha 负责原生撮合和账户。支持日频 JSON 规则和可信 Python 策略、多证券组合、分钟研究、持久任务及 paper 模拟；数据覆盖与可用运行模式以本次目录、预检和冻结回执为准。

当前候选版本 `0.12.0.dev1` 使用功能目录和同一份新版 A数达运行时。源码身份随实际文件内容计算，当前候选不继承历史独立验收结论。

## 安装指南

需要 Python 3.12。原生引擎安装前阅读 [许可证](docs/licensing.md)。从明确的 A数达候选构建并核对本地依赖 wheel：

```sh
python scripts/stage_owner.py --source /path/to/ashare-data
uv sync --frozen --extra native
```

安装使用已经配对的锁文件与 owner pin。修改数据依赖时按 [开发指南](docs/development.md) 更新。行情、凭据、账户和历史运行产物不随发行包发布。

## 快速开始

以下命令只创建新的合成演示状态：

```sh
ashare-quant --db .state/demo/control.sqlite3 demo
ashare-quant --db .state/demo/control.sqlite3 runs
ashare-quant --db .state/demo/control.sqlite3 capabilities
```

有已准备的 A数达本地数据时，在新控制库中创建任务：

```sh
export ASHARE_QUANT_DAILY_STORE=/path/to/new/data-store
ashare-quant --db .state/tasks/control.sqlite3 task-catalogue
ashare-quant --db .state/tasks/control.sqlite3 task-data-bind --dataset DATASET_ID
ashare-quant --db .state/tasks/control.sqlite3 task-create --request request.json
ashare-quant --db .state/tasks/control.sqlite3 task-worker --once
```

先从 catalogue 获取真实参数范围、数据绑定和假设摘要，再构造请求。缺数据、运行依赖或必要证据时，保留结构化拒绝信息。

## 使用指南

- [日频任务与策略](docs/tasks.md)：绑定、参数、可信代码、队列、结果及恢复。
- [组合和公司行动](docs/portfolio.md)：多证券导入、预检、原生组合和权益到账。
- [分钟与合成回测](docs/backtests.md)：模块入口、示例和边界。
- [paper 模拟](docs/paper.md)：新账户、观察输入、连续状态及恢复身份。
- [数据与诊断](docs/providers.md)、[开发与契约](docs/development.md)、[迁移说明](docs/migration.md)。

HTTP 本地入口为 `ashare-quant --db NEW_DATABASE serve`。日频任务前缀 `/api/v1/tasks/daily`，组合 `/api/v1/portfolio`，市场 `/api/v1/market`。paper 是独立应用。生成契约位于 [docs/contracts](docs/contracts)。

## 限制与说明

这是本地可信策略环境，不是任意代码沙箱或实盘券商。历史来源、PIT、分钟完整性、价限与公司行动的未知项必须保留；合成数据不能代表真实市场验收。金额保持十进制字符串，股数和成交由原生账本决定。

新版使用新数据库、新任务和新账户。旧回执、恢复状态、封存源码及历史审阅摘要不改写；既有运行包继续绑定原环境。
