# 文档索引

本页标明哪份文档描述当前行为，哪份保留历史。生成契约在 [contracts](contracts)，以 OpenAPI 为准。

## 正式本机入口

正式入口是 ashare-local，与 quant-web 按同一份部署清单配对。默认端口：API `18839`、Web `41839`、paper `18818`。候选或隔离验收清单里的其他端口不是这个正式入口。启动、备份和 `commands` 环境变量见 [数据、启动与恢复](data-runtime.md) 与仓库 README。

A数达 wheel pin 和未发布候选状态 `unpublished_candidate` 由 AD 流水线写入 `src/ashare_quant/providers/data-runtime.json`。本文档不另定 pin 版本。

## 现行指南

策略怎么写、怎么提交，以 [Python 策略工作台](python-workbench.md) 为准。下面三份是配套 HTTP 契约，各管一块：

| 文档 | 范围 |
| --- | --- |
| [strategy-files.md](strategy-files.md) | 单文件目录、读写与 `X-Quant-Workspace` 工作空间 |
| [strategy-projects.md](strategy-projects.md) | 多文件项目 |
| [strategy-backtests.md](strategy-backtests.md) | 回测上的策略身份 |

其余现行说明：[任务](tasks.md)、[因子](factors.md)、[分钟回测与来源规则](backtests.md)、[账户](paper.md)、[结果分析](result-analysis.md)、[Research MCP](research-mcp.md)、[依赖](dependencies.md)、[开发](development.md)、[许可证](licensing.md)、[数据运行与产品查询](data-runtime.md)。

## 历史与迁移

这些页面保留当时的记录，页首有说明。部署配对从仓库 README 和 [数据运行](data-runtime.md) 读取。

| 文档 | 地位 |
| --- | --- |
| [formal-backend-migration.md](formal-backend-migration.md) | 历史目录演变、已退役路由的删除记录，以及当时的前端切换清单。 |
| [aquant-migration.md](aquant-migration.md) | 已保存策略的 `jqdata` → `aquant` 导入改写工具。运行时写法见工作台。 |
