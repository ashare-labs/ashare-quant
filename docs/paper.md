# Paper 模拟与恢复

`python -m ashare_quant.paper --help` 管理本地模拟；`paper.api:create_app` 提供独立 `/api/v1/paper` 应用。[HTTP 契约](contracts/paper.openapi.json) 使用临时空根生成。

流程：创建新身份，绑定数据与策略，验证风险和假设，按显式交易日生成意图，推进原生执行，收盘检查账户与检查点。synthetic_fixture 和 historical_research 必须保留标签；历史回放成功不授予实时观察或实盘权限。

paper.online_cli 处理日历观察与计划，paper.intraday_cli 处理分钟观察，paper.continuation_cli 和 paper.successor_cli 处理显式前序绑定的延续。先看 --help，再指定新账户根。来源 readiness、行动范围和开收盘边界依据真实证据，不能修改日期或旧回执补齐缺证。

输入、源码、环境与消费契约逐项冻结。新版只有一份已安装 AD 候选，子进程保持离线和只读边界。风控、T+1、预算、分红与股份规则由原生能力执行；控制层保存意图、租约、状态和投影。

恢复先核对源码、策略、数据、计划和状态摘要。runtime_migration_cli 只操作满足条件且明确选择的实验副本，不修改历史验收状态。没有额外指令，不为升级迁移真实账户。旧运行包、审阅凭证和账户继续使用原身份。
