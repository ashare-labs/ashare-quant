# 开发脚本说明

所有维护脚本从仓库根目录在锁定环境中通过 `python scripts/<name>.py` 调用：

| 脚本 | 用途 |
| --- | --- |
| `run_tests.py` | 运行 `pytest` 并统一管理测试及子进程的隔离临时目录，测试结束后自动清理 |
| `check_structure.py` | 检查模块分层导入、文件命名、版本一致性、`uv.lock` / AD 运行时 pin 摘要、生成契约哈希与 Markdown 链接 |
| `stage_owner.py` / `pin_data_runtime.py` | 从指定的 `ashare-data` 源码构建候选 wheel，并更新 `src/ashare_quant/providers/data-runtime.json` |
| `freeze_runtime.py` | 生成或校验（`--check`）core 与 `--native` 运行时依赖快照及 `src/ashare_quant/lock.sha256` |
| `generate_contracts.py` / `export_mcp.py` | 基于临时空库导出 `api`、`tasks`、`paper` OpenAPI 契约、`identity.json` 与 `research-mcp.tools.json` |
| `verify_wheel.py` | 在无 `src/` 源码树的新虚拟环境中按锁定哈希安装依赖与 wheel，验证运行身份与核心回归测试 |
| `package_runtime.py` / `rebuild_runtime.py` | 打包配对的 AD/AQ wheel 与锁定依赖清单 / 在新空目录离线重建运行环境 |
| `validate_research_mcp.py` | 使用显式计划对 `stdio` 与 `streamable-http` MCP 传输执行端到端集成校验 |

完整更新顺序与参数说明见 [开发、依赖与许可](../docs/development.md)。
