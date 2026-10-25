# 开发脚本

从仓库根用当前锁定环境运行 `python scripts/<name>.py`；脚本不是产品 CLI 的替代入口。
输出使用显式新目录。运行时和真实数据不放进发布树，不把脚本中的合成输入当成真实市场验收。

| 任务 | 入口与用途 |
| --- | --- |
| 日常回归 | `run_tests.py` 管理整个 pytest 及其子进程的临时目录；tests/conftest.py 隔离单个用例的部署选择，两者作用域不同 |
| 结构与链接 | `check_structure.py` 检查产品导入、共享层依赖、文件命名、版本、生成契约摘要和 Markdown 本地链接 |
| 生成契约 | `generate_contracts.py` 使用空临时数据库导出 api/tasks/paper OpenAPI；`export_mcp.py` 导出 SDK tools/list |
| 安装验证 | `verify_wheel.py` 在新环境按锁安装依赖、安装非 editable wheel，再运行无 src 目录的测试 |
| 原生模型验证 | `verify_portfolio.py`、`verify_paper.py`、`verify_paper_research.py`、`verify_corporate_actions.py`、`verify_dividend_boundary.py` 保留各自模型、输入与证据语义 |
| 数据/结果对照 | `verify_diagnostics.py`、`compare_portfolio_reference.py`、`portfolio_reference.py` 读取显式输入，不替代数据覆盖或当前任务回归 |
| MCP 集成 | `validate_research_mcp.py` 使用显式计划与已有发布，启动本次临时传输并关闭 |
| 已绑定账户操作 | `prepare_paper_execution.py`、`rebalance_owner_preflight.py`、`run_rebalance_foreground.py`、`response_replay.py`、`replay_sealed_responses.py` 保留各自冻结会话与审核条件 |

## 依赖与运行时顺序

1. 更新 AD 时，`stage_owner.py` 从指定源码构建候选 wheel，`pin_data_runtime.py` 固定提交、文件清单和 wheel 身份。
2. 按[依赖指南](../docs/dependencies.md)更新 `uv.lock` 并同步环境。仅有源码/文档重构时不重建依赖。
3. `freeze_runtime.py` 和 `freeze_runtime.py --native` 分别生成 core/native 依赖快照；日常验证使用 `--check`。
4. 构建 AQ wheel，运行 `verify_wheel.py`，再由 `package_runtime.py` 保存配对 wheel、锁定依赖与恢复脚本。
5. `rebuild_runtime.py` 只安装到不存在的新目录，核验恢复身份，不替换现有解释器。

完整参数以各脚本 `--help` 和[开发指南](../docs/development.md)为准。脚本路径、产品 CLI、
`python -m ashare_quant.paper` 及现有 `paper.*_cli` 模块保持独立入口；共用实现不改变调用格式。
