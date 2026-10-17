# 开发、构建与契约

先读 [贡献规范](../CONTRIBUTING.md)。功能目录为 backtests、strategies、portfolio、execution、paper、tasks、market、storage、providers。tests 保留最小 fixtures，历史批次材料不发布。

```sh
python scripts/check_structure.py
python scripts/freeze_runtime.py --check
python scripts/freeze_runtime.py --native --check
ruff check src scripts tests
python -B -m pytest -q
python scripts/generate_contracts.py --output docs/contracts
uv build
```

外部真实证据测试由环境变量提供隔离输入；缺输入明确 skip，不表示通过。原生测试使用临时目录和新虚拟账户。资源、动态导入、策略摘要或恢复绑定变化时，把 wheel 装入独立环境重跑对应测试和 CLI；检查 wheel、sdist 清单与文档链接。

`scripts/verify_wheel.py WHEEL --output NEW_DIRECTORY` 会创建新环境、按锁定摘要安装依赖和 wheel，核对源码/安装身份，再运行不含源码目录的原生执行、恢复和契约用例。可加 `--offline --cache CACHE_DIRECTORY` 使用明确缓存；`--daily-components COMPONENT_DIRECTORY` 加入调用者提供的真实日频输入。它不会启动 HTTP 服务。

契约生成不启动服务，只使用临时库和空 paper 根。tasks/portfolio/paper/api OpenAPI 与 identity.json 是前端对接来源；最终提交前重生成并核对源码身份和 schema，不手工修改 JSON。

依赖的声明、唯一锁文件、运行时快照和按需导出各有独立职责，见 [依赖指南](dependencies.md)。更新依赖时先改 pyproject 的约束；若更新 AD，再用 scripts/stage_owner.py 从明确源码构建 wheel、scripts/pin_data_runtime.py 固定真实身份。运行 `uv lock`、`uv sync --locked --extra native` 后，分别运行 `python scripts/freeze_runtime.py` 和 `python scripts/freeze_runtime.py --native` 生成快照，再用上面的 `--check` 命令核对。没有依赖变化时不重建锁或快照。

依赖锁及运行时快照参与源码和恢复身份；它们改变时重生成契约并交接新的 identity，不覆盖旧账户。仓内没有 CI 配置；以上检查是本地验收入口，`verify_wheel.py` 也在干净安装中执行两种快照检查。不得同时装旧 AD 核心和另一套读端。回放、审计统一 scripts，输出写明确新目录。
