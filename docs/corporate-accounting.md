# 公司行动日期级账户处理

`0.11.0.dev23` 接收 AD `CorporateHistoryView.export_aq` 的 v1 schema、v2 稳定权益键。现金、股份到账、股份解锁分别写入原生 RQAlpha StockPosition 的持久状态，现金由原生 Account 接收，撮合和交易流水仍由原生 broker 产生。继承 dev21 的冻结数据与策略身份校验；不更改旧题、已有账户或聚宽参考。

## 输入和来源

在独立的 AD 环境使用新 reader，把完整事件响应附到既有真实日线导出。AQ 执行时不导入该新 AD reader，运行依赖保持原 pin；准备环境需另装经审阅的 AD `0.9.1.dev13`（候选 `09ff2a308414738601fa9d785e9fb748418102d5`）。不修改 AQ 运行环境的 owner 依赖。

```sh
PYTHONPATH=/path/to/aq/src /path/to/ad-python -m ashare_quant.daily_portfolio.corporate_export \
  --input /private/owner-prices.json --binding /private/package/binding.json \
  --binding-sha256 SHA256 --start 2022-05-20 --end 2022-06-17 \
  --accept-date-model --output /private/corporate-input.json
```

输出保留原价格、历史查询摘要、状态证据和被替代的旧行动资料，另存完整 coverage、lineage、revision IDs 与响应摘要。AD 包由其公共 reader 验证。`ready` 只说明来源范围和日期字段可供研究；AQ 再校验税模型、日期顺序、阶段键、来源问题、覆盖数量、窗口、证券和账户假设。未知/冲突/缺失、配股、重整和换码均阻断；不能因选择更短窗口而丢弃未解决来源问题。发行人总股本不用于增加投资者持仓。

## 账户规则

| 阶段 | 原生处理 | 验证重点 |
|---|---|---|
| 登记收盘 | 固定当日成交后的持股基数、现金额和新增股数，保存稳定键及来源版本 | 当日买入有权、登记后买入无权；以后卖出不能改变基数 |
| 除权日开盘前 | 确认应收现金和待到账股份；原价估值另含待到账股份价值 | 未到账不等于无权益，不重复计入到账持仓 |
| 支付日开盘前 | 向原生 Account 返回一次现金增量，清除应收 | 完整 Decimal 每股金额 × 原基数；无预先分位截断 |
| 股份到账日开盘前 | 增加原生数量与持仓队列，保存受限股数 | `share_credit_date` 单数字段；新增比率为 bonus + capitalization |
| 可售日开盘前 | 解除该批股份限制，不再次增股 | 受限股与当日新买 T+1 分别扣除；旧股仍可售 |

日级模型明确假设以上到账在对应交易日开盘前可见。这不是实际日内到账证明，也不是历史 PIT 或策略公告可见性证明。纯策略仍只收到前一日历史和上一账户净值，收不到事件表。

现金采用税前无扣税研究口径，平均成本在除权时按税前现金与新增股份重新分配，延后到账股保留相应成本。目前只支持登记日与除权日相邻交易日、除权不晚于到账不晚于可售、同证券同登记日一个分配方案。窗口内各阶段必须落在输入交易日历。非整数新增股直接报 `CORPORATE_FRACTIONAL_ENTITLEMENT_UNSUPPORTED`，不猜测登记机构的零碎股分配。普通分红不能替换为除权价格扣减系数。

不同登记日的计划允许阶段交叠。每天先以昨收持仓核对和处理全部当日除权，再处理现金、股份到账与解锁；旧计划今天到账的股份不会改写新计划昨天的登记基数。旧计划待到账股份保留自身成本，新计划只在其有权持股及新股间分配成本；暂用的除权估值包含全部已到账和待到账股份，到账本身不创造盈亏。同证券同登记日的不同方案仍因组合规则未定义而在预检查报 `CORPORATE_SAME_RECORD_DISTRIBUTIONS_UNSUPPORTED:证券:登记日`，创建任务时不生成账户或原生 checkpoint；不会将一般日期交叠一律拒绝。

每个持仓保存登记、除权、现金、到账、解锁阶段和事件原文；零持股但有未结权益的持仓不会被清除。每个完整事件后的原子 checkpoint 同时保存原生账户与权益状态，中途终止回退到前一完整事件。重复事件按相同稳定键去重；同键不同内容报 `CORPORATE_REVISION_RECONCILIATION_REQUIRED`。已开账户的输入、版本或策略变更由冻结身份拒绝，当前没有对已结算来源更正的自动冲正功能。

新账户的执行窗口必须含相关登记日。若选择从登记后开始且仍有待结权益，报 `CORPORATE_RECORD_BASIS_OUTSIDE_RUN`；不得用当前仓位推断原基数。跨日恢复使用原 checkpoint 和原输入。不同来源包、不同窗口之间迁移未结权益未开放，不能用后续空查询替换旧账户状态。

## 执行与结果

既有 `daily_portfolio` CLI 可直接执行该导出；回溯状态仍需 `--accept-owner-state-model`。公开组合 API 可导入该文件，预检、冻结、独立 worker、发布、重开读取和恢复使用同一输入。若请求 `price_only_no_limits`，正式事件导出会明确拒绝，避免无声丢弃公司行动。历史官方价限缺证仍是缺证，新接口不会补造它。

原生结果新增 `corporate_entitlements`、事件响应摘要和明确模型。持仓增加 `restricted_shares`、`pending_shares`、`pending_share_value`；API 类型为这三个字段声明零默认值。`corporate_daily_pnl` 保存原生当日盈亏，待到账股份随原价逐日计价，到账只转为持仓，不再产生一次收益。独立 Decimal 审计从初始条件、原始事件、成交逐日重算现金、总股数、受限股、T+1 可售、待到账价值及阶段日志，并核对原生当日盈亏等于独立净值变化，发布必须通过此审计。

dev23 新运行无论使用旧格式输入或正式公司行动输入，原生 `final_account.positions`、`accounts[].positions` 和 API 对应投影都显式输出上述三字段，且对应值逐字一致。旧格式输入为整数 `0`、整数 `0`、字符串 `"0"`；正式输入返回实际值（金额仍是十进制字符串，可含 `"0.0"`）。这些默认值不授权修改旧发布证据，也不表示可以在比较时忽略非零值。前端应同步这三个已知 schema/类型并保持身份、required、类型及原生证据一致性检查，不忽略任意新字段。`value` 仅含已到账股份市值，待到账价值及应收股息另计入净值；数量为零但仍有这两类权益的持仓应继续展示。正式结果的 `corporate_action_policy` 按本日期模型输出，旧格式结果保留原 policy 文案。

针对停牌分红等规则验证，独立 CLI manifest 可声明 `execution.initial_positions={证券:整数股数}`、`initial_position_model="research_seed_previous_close"`；按前一日原价初始化原生持仓，现金独立指定。这是明确的初始研究假设，不创建虚假买入流水。公开任务 API 不开放持仓种子，仍从指定现金起步。

## 可执行覆盖

```sh
PYTHONPATH=src python -m pytest -q tests/daily_portfolio/test_corporate_actions.py
PYTHONPATH=/path/to/ad-reader:src python scripts/verify_corporate_history.py \
  --binding /private/package/binding.json --binding-sha256 SHA256 \
  --library /read-only/ashare-data-local --output /private/new-evidence
ASHARE_QUANT_TEST_CORPORATE_INPUT=/private/w2022-corporate.json \
  PYTHONPATH=src python -m pytest -q tests/portfolio_jobs/test_portfolio_api.py \
  -k formal_corporate
```

真实验收脚本使用已存 AD 原价和明确的回溯 5%/10% 价限模型，只读查询、无采集。固定包含八股 2020–2025 全部 67 次普通现金事件、隆基持有/分日卖出/受限卖出尝试、000022 停牌现金分红。每例保存输入与来源摘要、原生持仓/成交/净值、独立审计、进程耗时和内存。它检验该研究模型下的账户处理，不认证历史官方价限、税后收益、实际买入成本或聚宽对齐。

合成规则另覆盖缺失日期、矛盾、未知来源、碎股、同键更正、零登记持仓、多个登记基数与不同支付日、卖完旧股后延迟到账、T+1 与解锁叠加、阶段中断、原子替换及重复运行。真实证据不写入发行包，源码包仅包含可复跑脚本与合成测试。
