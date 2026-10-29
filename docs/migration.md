# 历史目录迁移与当前入口

> 历史目录说明。现行文档地图见 [索引](README.md)。部署端口与 quant-web 配对见仓库 README 和 [数据运行](data-runtime.md)。

本页保留旧文档链接，记录目录演变。现行 HTTP 以 [contracts](contracts) 为准。已退役路由的删除记录见 [正式研究接口](formal-backend-migration.md)。早期迁移的固定窗口、模板策略和资源门槛不适用于当前 Python 工作台。

## 已完成的目录演变

AQ `2aa7829` 与 AD `2fe79d9` 的共同整理确立了功能目录、snake_case Python 命名、kebab-case 文档、五章 README、真实数据不入发布树，以及源码和依赖变更产生新身份的原则。现行贡献规范见[贡献指南](../CONTRIBUTING.md)。当时的包版本、窗口和路由只描述历史，不是今天的容量或入口约束。

早期 `m2`、`daily_portfolio`、`rqprobe` 的职责分别进入 backtests/tasks、portfolio 和 execution。`m2job-` 等序列化业务标识仍保留原含义；不要机械改写旧回执、数据库记录或检查点。

`/api/v1/tasks/daily/**`、`/api/v1/portfolio/**` 和固定七模板运行入口已退役。`two_day`、`w1`、`w2` 仅属于历史证据，不能用于创建当前研究。当前日频/分钟统一由 `/api/v1/python/jobs` 提交用户代码，不要求固定股票池、额外提交证券池或模板策略；策略按需查询已配置数据。资源与磁盘设置见[任务服务](tasks.md)，可用策略 API 见[Python 工作台](python-workbench.md)。

策略导入使用 `aquant`，旧 jqdata 等别名的离线转换见[导入迁移](aquant-migration.md)。不恢复已删除的 Python 模块或策略别名。当前仍存在的 CLI/Python/API 入口则属于兼容契约，不能借早期“无旧名 shim”条款删除。

## 历史与交付身份

旧任务和结果通过 `/api/v1/history` 读取；源码变更不授权当前 worker 执行旧任务，也不改写旧文件来继承验收。账户、数据集、冻结发布和模拟运行时保留原身份。数据库升级只在单独核查和显式选择后进行，不因目录整理自动迁移。

当前 HTTP 契约为 api/tasks/paper 三份 [OpenAPI](contracts)，另有研究 MCP 工具契约。生成流程见[开发指南](development.md)。早期四份 schema、旧 CLI 和固定窗口说明已由这些当前契约取代。
