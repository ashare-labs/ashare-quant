# 当前产品迁移

固定窗口、七算法与演练入口已退役，当前切换要求见 [正式后端迁移](formal-backend-migration.md)。以下仅记录更早的目录演变，不作为当前接口调用指南。

# 功能布局迁移

新版没有旧名 shim。m2 的契约迁入 backtests.daily，策略迁入 strategies.daily，数据适配迁入 providers.daily；tasks 拆为共享 tasks 和 tasks.daily；m2_native 迁入 execution.daily；portfolio_jobs 迁入 tasks.portfolio。daily_portfolio 迁入 portfolio，minute 迁入 backtests.minute，rqprobe 迁入 execution.rqalpha。

共享锁、原子写入和序列化位于 storage，订单预算位于 execution.reservation，当前结果投影位于 tasks。round_one、second_batch、execution_six、d1、br1_fixed 及封存结果服务退出产品；有用验证器和最小 fixtures 留在 tests/scripts，完整历史材料不搬成 archives。

HTTP /api/v1/m2 改为 /api/v1/tasks/daily，/api/v1/research 改为 /api/v1/market。CLI job-* 改 task-*，jobs 改 tasks，research-* 改 market-*。组合与 paper 功能前缀保留。

算法 ID 和部分 wire 标签保留语义，例如 `daily_portfolio.v1`、`m2-jobs.v6`、`m2-job-manifest.v6` 和 `m2job-` ID 前缀。这些是序列化业务标识，没有旧 Python 模块别名。AD 的旧窗口 `m2a` 明确改为 `two_day`，`w1`、`w2` 保留；请求必须使用当前 catalogue 返回的窗口，不能机械改写旧回执。

源码路径、模块名、策略 SDK 摘要及包版本都改变，因此新版产生新任务、账户和恢复身份；旧库不能只换目录后继续运行。AQ `0.12.0.dev6` 固定 AD `0.10.0.dev5` 的候选提交、wheel 及代码清单；所有数据域共用这一安装。新身份是 `not_reviewed`，不复用旧 PASS。

可信 Python 策略将 `ashare_quant.strategy_api` 改为 `ashare_quant.strategies.api` 后重新注册；组合策略将 `ashare_quant.daily_portfolio.signals` 改为 `ashare_quant.portfolio.signals`。新的源码字节与 SDK 摘要会产生新策略版本，不覆盖旧任务封存的策略。RQAlpha 适配器类路径也已改变，旧检查点只交给原固定环境恢复。

旧 fixed/parameters/bound-data 准备页退出，可执行能力集中于 Tasks（准入日频域）与 Portfolio（覆盖的多证券域），分钟仍为本地 CLI。日频 Tasks 包含固定窗口、参数及结束日期、规则/可信 Python 版本、既有数据绑定、任务日志/结果、取消和恢复；文件注册及数据导入仍通过本地 CLI。D1/BR1 历史执行及静态封存结果停供，不将它们声明为新日频任务的等价重放。

验收验证实际注册、导入/绑定、原生账本、失败拒绝、崩溃恢复、取消恢复、API 投影与 wheel 加载。历史回执和封存包保持原字节，不给新源码套用旧审阅 hash。

控制数据库使用 schema 4 的功能型任务表。旧数据库按原后端继续读取；新版本不原地迁移旧任务或账户。

前端用 `scripts/generate_contracts.py` 导出的四份 `docs/contracts/*.openapi.json` 和 `identity.json` 作为完整集合。API、daily、portfolio、paper 的运行时 OpenAPI 与导出共用入口；paper `/api/v1/paper/schema` 返回其当前 OpenAPI。更新前端时先校验四个文件摘要，再生成类型。源码摘要变化不能只改 identity 文件而保留不一致契约。

周一模拟继续使用其已封存代码、解释器和账户。此次候选不迁入封存包、不重刷其源码哈希、不修改恢复绑定；新布局验证不代表旧模拟已换用新版。
