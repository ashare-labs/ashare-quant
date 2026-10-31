# 文档索引

本目录包含 `ashare-quant` 的功能指南与自动生成的接口契约（[`contracts/`](contracts)）。

## 正式本机入口

正式本机部署（`ashare-local`）与 `quant-web` 共用同一份 `deployment.json` 清单，默认端口为：API `18839`、Paper `18818`、Web `41839`。配对的 `ashare-data` 版本与 wheel 摘要记录在 [`src/ashare_quant/providers/data-runtime.json`](../src/ashare_quant/providers/data-runtime.json)。

## 功能指南

| 文档 | 核心内容 |
| --- | --- |
| [data-runtime.md](data-runtime.md) | `ashare-deployment.v1` 清单配置、服务启动、备份与空目录恢复、`/api/v1/product` 行情/财务/分钟查询 |
| [python-workbench.md](python-workbench.md) | `aquant` 策略编写、单文件与多文件项目、工作空间（`X-Quant-Workspace`）、回测归属列表及 `jqdata` → `aquant` 迁移工具 |
| [backtests.md](backtests.md) | 通用分钟与日频回测模型、行级来源准入规则、共享异步任务队列、流式输入与存储预算 |
| [factors.md](factors.md) | 368 项因子目录（聚宽 19 + Alpha191 + Qlib Alpha158）、数据源配置、异步计算与 Alphalens 评价 |
| [result-analysis.md](result-analysis.md) | 冻结结果派生分析（`returns-v1` / `returns-v2`）、20 项绩效指标定义、基准比较与交易盈亏归因 |
| [paper.md](paper.md) | `/api/v1/paper` 只读账户历史、已结算 v3 账本运行时升级授权与持仓续接 |
| [research-mcp.md](research-mcp.md) | Agent Research MCP 适配层、13 个研究与因子工具、幂等提交与分页协议 |
| [development.md](development.md) | 依赖锁定、运行时快照、契约生成、wheel 验证、软件许可及历史退役路由对照表 |
