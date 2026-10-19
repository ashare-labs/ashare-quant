# Paper 模拟与恢复

`python -m ashare_quant.paper --help` 管理本地模拟；`paper.api:create_app` 提供独立 `/api/v1/paper` 应用。[HTTP 契约](contracts/paper.openapi.json) 使用临时空根生成。

流程：创建新身份，绑定数据与策略，验证风险和假设，按显式交易日生成意图，推进原生执行，收盘检查账户与检查点。synthetic_fixture 和 historical_research 必须保留标签；历史回放成功不授予实时观察或实盘权限。

paper.online_cli 处理日历观察与计划，paper.intraday_cli 处理分钟观察，paper.continuation_cli 和 paper.successor_cli 处理显式前序绑定的延续。先看 --help，再指定新账户根。来源 readiness、行动范围和开收盘边界依据真实证据，不能修改日期或旧回执补齐缺证。

输入、源码、环境与消费契约逐项冻结。新版只有一份已安装 AD 候选，子进程保持离线和只读边界。风控、T+1、预算、分红与股份规则由原生能力执行；控制层保存意图、租约、状态和投影。

恢复先核对源码、策略、数据、计划和状态摘要。旧运行包、审阅凭证和账户继续使用原身份；模拟测试通过不会改变历史验收结论。`runtime_migration_cli` 处理已有 v2 延续绑定且尚未开始的账户，不能用于已结算 v3 账户。

## 已结算 v3 运行时迁移

`paper.settled_migration_cli` 显式接管 `declared-inventory-target-minute-partial-cancel.v3` 账本。所有已有交易日必须已提交，开收盘报告必须与原生检查点一致，结算次数、游标、未结订单和冻结现金均需通过校验。正在等待收盘、执行或恢复的账户不能迁移。

先在隔离副本上复核成交、佣金与税费、现金、持仓、T+1、重复调用和中断恢复，再明确选择已结算的目标账户。以下参数必须从实际文件和运行环境计算，不能复用另一账户或旧构建的摘要：

```sh
python -m ashare_quant.paper.settled_migration_cli \
  --root "$ACCOUNT_ROOT" --state-sha "$STATE_SHA" \
  --from-sha "$OLD_RUNTIME_SHA" --to-sha "$NEW_RUNTIME_SHA"
```

默认仅检查。确认结果后，相同命令增加 `--apply --review-sha "$REVIEW_SHA" --reason "$REASON"` 才写入授权。`STATE_SHA` 是原始 `state.json` 的 SHA-256；两个运行时摘要由 `storage.artifacts.digest` 分别计算原状态 `engine_pin` 与新环境 `paper.service.engine_pin()`；`REVIEW_SHA` 标识此次兼容性复核材料。必须显式授权具体真实账户才能应用，CLI 不自动发现或迁移账户。

授权记录完整的新旧源码与依赖指纹。Python、RQAlpha 源码和除 AD 外的原生依赖必须相同；本版本仅允许经过兼容验证的 AD `0.8.0.dev2 → 0.10.0.dev5` 变化，或 AD 版本不变的显式源码升级。当前 AD 包还必须匹配 AQ 内置的完整所有者文件清单和读取器摘要。没有移除指纹检查，也不把旧 AD 版本改写为新版本。

迁移保留原身份、配置、`engine_pin`、历史交易日与事件前缀，原生文件逐字节不变。原始状态按摘要存入 `runtime-origins/`，当前状态增加 `paper-settled-v3-runtime-authorization.v1` 授权与一条事件。相同选择重试幂等；中断后可重试，输入不一致则拒绝。完成后可读取或重复恢复已提交阶段，但不能直接准备新交易日。

## v3 实际持仓续接

新的持仓日需通过 `paper.successor_cli` 分别生成计划并显式绑定。v3 使用 `paper-declared-inventory-hold-successor.v1`，保留 v3 模型、原始申报库存和原始目标的历史含义。新日目标取自前一收盘原生账户的实际数量；不会重新注入申报库存，也不会补齐前一日因部分成交留下的目标缺口。v2 延续及其调仓日限制保持原语义。

交易日必须来自所有者日历，位于已复核行动窗口内；计划绑定新的所有者读取器与基线谱系，执行仍需当日真实观察和独立开收盘条件。运行时迁移不延长任何日期窗口，也不授予新交易日、自动化或实盘权限。下一日尚未获得有效所有者证据时应停止在已结算状态，不能修改日期或回执补齐缺证。

此入口接管显式指定的 `OnlineService` 账本目录，不会自动导入产品控制数据库、部署服务或创建定时任务。原始运行包和历史账户证据的清理另行决定。
