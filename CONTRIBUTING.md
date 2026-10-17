# Contributing

按业务功能组织代码，不用开发轮次、验收题号或临时版本作为产品目录。产品不得导入 tests、examples、scripts 或历史报告。共用运行能力放 storage、execution 或 tasks；验证器放 tests 或根目录 scripts。删除失去用途的材料，不建立 archives 替代设计判断。

Python 包、模块和 Python 脚本使用 snake_case；文档及普通非 Python 目录使用 kebab-case。公开协议、策略 ID 与数据标签变更须显式说明，不机械改写回执内容。README 保留面向人的五章结构。

开发脚本统一 scripts 入口；tests 只保留必要 fixtures；examples 应可运行；docs 只保留当前指南、参考和必要迁移说明。禁止提交个人绝对路径、凭据、真实账户、缓存和构建产物。

运行 `python scripts/check_structure.py`、`ruff check src scripts tests` 和受影响测试。契约变更运行 `python scripts/generate_contracts.py` 并审阅 diff。涉及资源、动态导入和恢复身份时，用实际 wheel 验证对应入口；不要为等价移动或文档修改增加无必要门槛。
