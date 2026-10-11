# 日频多股整链验收入口

入口为 `python -m ashare_quant.daily_portfolio`。这是独立的有界本机研究入口，使用一个 RQAlpha 原生账户和共享现金，不改变现有 `m2-jobs.v6` 单股协议，也不接入持续模拟或券商。

题面见 [manifest](../examples/daily_portfolio/manifest.json)。三个可信纯函数策略只接收截至前一开市日的 120 个原始收盘价和前一已提交权益。98% 目标权重按前收盘换成整手数量，调仓先卖后买。日频开盘价撮合不宣称开盘集合竞价排队、成交容量或真实可交易性。没有读取当日成交量决定开盘容量。

```sh
PYTHONPATH=src .venv/bin/python -B -m ashare_quant.daily_portfolio \
  --manifest examples/daily_portfolio/manifest.json \
  --input /path/to/ad-export.json --strategy momentum --window w2020 \
  --output .state/daily-portfolio/momentum-w2020
```

先加 `--preflight` 检查逐证券、日期和字段缺口。缺价、缺历史停牌/涨跌停状态、未覆盖公司行动会中止并写 preflight；不会填零、删证券或换窗口。输入只接受 `ad-daily-portfolio.v1` 的不可变 AD 导出（合成测试必须明确 `real_market_data=false`）。字段包括 producer、source_refs、limitations、securities、calendar、daily、actions、action_coverage、listing_coverage、price_basis=raw、volume_unit=share；daily 每行保留 date/security/raw OHLC/volume/suspended/upper_limit/lower_limit。源证据和导出只留本地，不能随公开代码分发。

通过独立 AD 环境中的公共 `DailyResearchView` 准备输入：把本 AQ 的 `src` 和已验收 AD 的 `src` 加入该进程 `PYTHONPATH`，运行 `python -B -m ashare_quant.daily_portfolio.owner_export --binding /path/to/binding.json --binding-sha256 SHA256 --window w2020 --output /path/to/ad-export.json`。此转换只调用 AD 公共 history/opening/closing/state/corporate_actions/read_window，不采集或重建数据层。保留每次调用摘要并逐日将 AQ 信号历史与 AD 查询结果摘要对账。

历史盘前状态缺失时，默认严格路径保留空值并阻止执行。需要显式研究模型时，导出和运行两步均加 `--accept-owner-state-model`。结果保留 `state_mode=retrospective_model`、完整假设和 `economic_acceptance=conditional_research_only`；这是对有限研究范围的选择，不能解释为已取得真实历史盘前状态或严格 PIT。停牌前收盘延用只能来自 AD 有独立公告支持的明确标记行，不能由 AQ 填充。

每次输出保留题面与源码清单、运行时版本、输入摘要、逐日信号和输入摘要、目标、订单、成交、原生账户、权益 CSV 和独立审计。原生数值字符串与展示保留不同职责：不能仅用两位小数判断一致。浮点账本核算容差固定为 `0.000001 CNY`。

公司行动使用明确的研究模型：在来源除息日确认税前应收，在来源派息日到账，股息扣税固定为零；不声称符合个人实际税率。股票变动要求入账、可交易与除权日一致，零碎股四舍五入。非相邻登记/除息日、配股、换股和其他事件拒绝进入。源覆盖不足仍为数据缺口；没有把旧历史研究假设提升成严格 PIT。原生账本与独立只读计算分别核对现金、应收、股数和权益。

同目录重新执行会校验数据、题面、策略、全部 AQ 源码及运行时身份。完整运行直接返回已提交结果；未完成运行用新的 `--invocation recover` 恢复原生 checkpoint。不同身份拒绝恢复，不能覆盖旧结果来消除失败。单目录文件锁保护写者。checkpoint 是可信本地 pickle，不接受来源不明的文件。

工程验收可离线运行：

```sh
.venv/bin/python -B scripts/verify_daily_portfolio.py --output .state/daily-portfolio-check-001
```

该脚本只生成明确标记的合成星期与价格，实际跑三策略、原样重跑、完成后重入、四个 SIGKILL 恢复点、最小佣金、共享现金拒单和未来价格扰动；合成结果不计真实策略或 JQ 验收。真正的验收还需要 AD 真实来源包、固定三窗口、旧原题回归及独立参考。

参考脚本为 [jq_reference.py](../examples/daily_portfolio/jq_reference.py)，默认等权和 2020 固定窗口。只按 manifest 切换 STRATEGY/WINDOW，同时在平台界面逐次核对日期、日频和初资。聚宽运行时的日频标识为 `day`，行情查询仍使用 `daily`；脚本记录实际返回的标识并拒绝分钟频率。每个组合使用独立文件名，完整事件通过平台支持的 `write_file` 输出。浏览器日志可能无法完整读取长记录，必须核对原生 JSONL 连续序号、每日回调覆盖和终态事件；可取得原生导出摘要时逐字节校验。免费时长与积分余额须现场检查，不以付费导出补齐缺失证据。

使用以下只读入口比较已保存参考和本机结果，不会采集数据、修改输入或建立第二份账户账本：

```sh
PYTHONPATH=src .venv/bin/python -B scripts/compare_daily_portfolio_jq.py \
  --reference /private/jq/events.jsonl --aq-result /private/aq/result.json \
  --owner-input /private/ad-export.json --manifest examples/daily_portfolio/manifest.json \
  --output /private/comparison.json
```

报告分开列示：用 JQ 历史输入及其前日权益复算的纯信号检查；JQ 与独立 AD 来源在实际观察字段上的差异；目标、去重成交、费用、现金/权益、数量、可售、成本和持仓估值。现金残差另列，不能自动改写为股息或税。报告生成成功不表示经济一致，必须阅读各差异列表。参考对齐也不证明历史状态、源端当时可得性或未观察字段已齐备。账户引用只保存在用户私有证据目录，不进入公开仓库。

前端消费契约：显示输入来源、窗口和策略身份，以及真实/合成、历史 PIT 未认证、实际参考验收状态和数据缺口；分别展示 signal.targets 和实际 positions。订单必须能追到 decision 日期、trade.order_id、费用、拒单及现金前后。权益曲线读取 equity.csv 或 result.accounts，末日与 final_account 对账；有 receivable 时独立列示。收益率应按同一已提交权益计算，不能把平台回调内可能尚未更新的 `returns` 属性当成同一时点的权益。恢复运行复用同一 identity 和订单/成交 ID，不能重复累计。前端尚无本入口注册，不得用旧单股 UI 冒称支持多股。

股息差异应沿字段来源归因。`cash_per_share` 是合资格持有人的每股毛现金权益；
`source_event.ex_price_cash_deduction` 是除息参考价扣减，两者可能因不参与分配的回购股而不同。
AD 导出保留完整 `source_event`，原生 `dividend_cash_before_tax / round_lot` 只接收前者。
盘前上下限由 AD 状态接口单独提供；不能用参考价扣减替换现金，也不能为贴近参考净额改写毛额。
原生平均成本按毛分红调整。只把净额作为输入可能对齐现金，却同时改变成本。
当前模型不实现按投资者类别和持股批次的递延红利税，也不把未知到账时刻当成源事实。

公开的最小用例仅用合成数据实际运行同一个原生账户：

```sh
.venv/bin/python -B scripts/verify_daily_dividend_boundary.py --output .state/dividend-boundary-001
```

合成持仓为 3500 股，每股毛额 1 元，参考价元数据 0.936124 元。两个故意错误的现金输入
0.9361 和 0.74888 产生 223.65、655.27、合计 878.92 元的差额；第二个系数只是
`0.9361 × (1 − 20%)` 的算术控制，不宣称任何历史账户适用该税率。
用例还验证参考价元数据变化不改变现金权益、相同订单/数量、成本差异、延期到账应收和
应收期间 SIGKILL 后恢复。它不读取私有 AD/JQ 数据，不修改真实题面，也不构成真实九组参考验收。

[机器可读消费者契约](../examples/daily_portfolio/consumer_contract.json) 固定现有结果与参考报告的
身份核验、权益/应收方程、同快照收益率和差异展示规则。它是待前端接入的读契约，尚无新 HTTP
路由或已验收 UI。结果 v1 未提供独立股息流水表；前端必须将余额推导的现金变化标为只读归因，
不得伪造原生流水。已解释的经济差异继续显示；未知支付时刻继续为空；不能将“原生核算通过”
合并显示成“独立参考完全一致”。
