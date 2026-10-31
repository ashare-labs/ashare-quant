# 贡献指南

## 目录与命名约定

源码位于 `src/ashare_quant`，按业务职责划分为功能子包：`backtests`、`strategies`、`portfolio`、`execution`、`paper`、`tasks`、`market`、`storage`、`providers`、`analysis`、`deployment`、`mcp`，以及放置基础类型与纯校验的 `core`。

- **依赖方向**：`core` 仅依赖基础契约，不反向导入业务功能包；产品代码不得导入 `tests/`、`scripts/` 或历史验证材料。
- **命名规范**：Python 包、模块和脚本使用 `snake_case`；文档及普通非 Python 目录使用 `kebab-case`。不使用阶段或轮次代号（如 `m2`、`d1`、`br1`、`round_one`）命名产品模块。
- **整洁度原则**：仓库根目录不新建 `examples/`、`archives/` 或 `tools/` 目录；跨测试共用的轻量构造器集中在 `tests/support/`。不在仓库中提交个人绝对路径、凭据、真实账户或构建产物。

## 文档与契约维护

- `README.md` 保持「项目简介、安装指南、快速开始、使用指南、限制与说明」五章结构。
- HTTP 与 MCP 接口变更后，运行 `python scripts/generate_contracts.py` 与 `python scripts/export_mcp.py` 更新 `docs/contracts/` 下的生成契约，勿手工编辑 JSON 契约文件。

## 本地验证流程

日常测试统一通过 `scripts/run_tests.py` 运行（自动分配并清理隔离临时目录，如需排查失败现场可传 `--keep-temp`）：

```sh
python scripts/check_structure.py
python scripts/freeze_runtime.py --check
python scripts/freeze_runtime.py --native --check
ruff check src scripts tests
python scripts/run_tests.py -q
```
