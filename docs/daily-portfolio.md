# 日频多股整链验收入口

新增两个普通配置策略，示例题面在 `examples/daily_portfolio/diversified/manifest.json`。
`low-volatility` 每月首个交易日将过去20个简单收益的总体方差最低三股各配98%/3；
`momentum-low-volatility` 每周首个交易日将60日收益降序秩与20日收益方差升序秩相加，
选秩和最低三股各配98%/3。相同因子值使用相同竞争秩，最终并列按代码；零方差有定义。
这两个策略的收益、方差和动量排序键从原始十进制价格精确转换为有理数，比较时不舍入，
也不使用 epsilon 合并近值。方差在完成选股后才转换为50位 Decimal 展示；展示相同不代表
精确因子相等。非零相等方差及收益序列的排列不改变并列关系。
两者都只用前120个收盘和前一已提交权益，价格因子组合不包含财报因子。
这是常见数学规则的原创实现，沿用仓库授权范围；未复制第三方策略代码或以收益调参。

输入现在拒绝OHLC倒置、开盘/收盘越出高低区间、下限高于上限，以及真实输入声明合成上市覆盖。
这些检查在创建原生账户前完成；缺失状态及缺根日线继续阻断。

新历史库只有原始价量而没有公司行动覆盖时，默认严格路径仍会阻断。
价格路径诊断须由输入显式声明 `research_model=price_only_no_actions`、空 actions、
`action_coverage.status=unavailable`、`state_mode=retrospective_model` 及
`economic_acceptance=price_only_diagnostic_not_economic_acceptance`，并同时提供
`--accept-price-only-research --accept-owner-state-model`。没有这些输入声明时，选择开关也不会
放行公司行动缺口；价格、状态、日历及来源缺口不能由该开关豁免。
诊断只核模型内现金、交易和估值，不计分红送转，输出收益不能解释为历史总回报或经济验收。
模型限价必须在输入中注明来源及假设；入口不会推断不存在的状态或把复权因子当作行动账。
原有有公司行动覆盖的入口、费用、原生账户及JQ对照契约保持原语义。

新增有界策略入口：`weekly-reversal` 每周首个交易日选过去五个收盘收益间隔跌幅前两名，
并按代码打破并列；`daily-equal-weight` 每交易日对传入证券等权。两者总目标权重均为98%，
目标仍仅由前一已提交权益与前收盘换算整手，使用相同原生账户和卖后买顺序。
`execution.volume_ratio` 可显式设置在 `(0, 1]` 内的日线成交参与率，通过原生撮合器的
`volume_limit/volume_percent` 生效；未设置或为null时保持原来的不限制行为。该参数引用
完整日线成交量，是事后日频研究模型，不证明开盘可成交数量或盘口队列。限量模式以
原始十进制股数和比例计算严格整手上限 `floor(volume × ratio / 100) × 100`，扣除同证券
当日已经成交的股数，不再允许原生四舍五入将不足一手的额度扩大为一手。
R05本次三笔部分成交在AQ的09:30执行回调取消余量；JQ在委托返回及after_orders已观测为取消，
不能据此认定JQ内部精确时钟或全天全部撤单机制。下一次调仓根据实际持仓计算目标差。
原v1日终取消题面与首跑继续失败。另行版本化的v2只通过本情景的“返回时余量已取消”正向审计，
使用相同捕获作观察后重评，既不是新盲测，也不是完整对齐。题库已有v2材料，本次不重复覆盖题库。

固定候选 `db5163d0497b03177440a6ec0f332334c85ac5df` 已获独审有限通过。三例已保存的真实参考
共464条事件、39个日终和每端49笔成交：R04保留59条money差，R05保留59条money差及v1契约失败；
R06保留10条money差和期末AQ高于JQ的3853.201元权益差。其差额链为3817.024元股息现金差、
33元额外价款损失和3.177元额外费用，归因闭合不等于经济对齐。旧九例仍是8组有限一致及格力
878.92元差异，缺少新增原始字段的旧参考不升级为六层齐全。完整来源、独审与本地证据绑定见
[整合记录](daily-portfolio-integration.json)。

入口为 `python -m ashare_quant.daily_portfolio`。这是独立的有界本机研究入口，使用一个 RQAlpha 原生账户和共享现金，不改变现有 `m2-jobs.v6` 单股协议，也不接入持续模拟或券商。

原三策略题面见 [manifest](../examples/daily_portfolio/manifest.json)，新增小批题面见 [expansion](../examples/daily_portfolio/expansion/selection.json)。可信纯函数策略只接收截至前一开市日的 120 个原始收盘价和前一已提交权益。98% 目标权重按前收盘换成整手数量，调仓先卖后买。日频开盘价撮合不宣称开盘集合竞价排队、成交容量或真实可交易性。原三题默认不限制当日成交量；新增可选限量模式具有上文说明的事后研究边界。

```sh
PYTHONPATH=src .venv/bin/python -B -m ashare_quant.daily_portfolio \
  --manifest examples/daily_portfolio/manifest.json \
  --input /path/to/ad-export.json --strategy momentum --window w2020 \
  --output .state/daily-portfolio/momentum-w2020
```

先加 `--preflight` 检查逐证券、日期和字段缺口。缺价、缺历史停牌/涨跌停状态、未覆盖公司行动会中止并写 preflight；不会填零、删证券或换窗口。输入只接受 `ad-daily-portfolio.v1` 的不可变 AD 导出（合成测试必须明确 `real_market_data=false`）。字段包括 producer、source_refs、limitations、securities、calendar、daily、actions、action_coverage、listing_coverage、price_basis=raw、volume_unit=share；daily 每行保留 date/security/raw OHLC/volume/suspended/upper_limit/lower_limit。源证据和导出只留本地，不能随公开代码分发。

通过独立 AD 环境中的公共 `DailyResearchView` 准备输入：把本 AQ 的 `src` 和已验收 AD 的 `src` 加入该进程 `PYTHONPATH`，运行 `python -B -m ashare_quant.daily_portfolio.owner_export --binding /path/to/binding.json --binding-sha256 SHA256 --window w2020 --output /path/to/ad-export.json`。此转换只调用 AD 公共 history/opening/closing/state/corporate_actions/read_window，不采集或重建数据层。保留每次调用摘要并逐日将 AQ 信号历史与 AD 查询结果摘要对账。

历史盘前状态缺失时，默认严格路径保留空值并阻止执行。需要显式研究模型时，导出和运行两步均加 `--accept-owner-state-model`。结果保留 `state_mode=retrospective_model`、完整假设和 `economic_acceptance=conditional_research_only`；这是对有限研究范围的选择，不能解释为已取得真实历史盘前状态或严格 PIT。停牌前收盘延用只能来自 AD 有独立公告支持的明确标记行，不能由 AQ 填充。

每次输出保留题面与源码清单、运行时版本、输入摘要、逐日信号和输入摘要、目标、订单、成交、原生账户、权益 CSV 和独立审计。新增原生订单事件时点、提交返回对象及下单前后/日终快照，均随checkpoint恢复。审计分别列会计核算、严格参与上限与冻结契约；契约不符或缺少必要时点证据时，保留完成结果并返回退出码3。原生数值字符串与展示保留不同职责：不能仅用两位小数判断一致。浮点账本核算容差固定为 `0.000001 CNY`。

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

报告保留原来的有限观察比较：用 JQ 历史输入及其前日权益复算信号，以及来源、目标、费用、现金/权益、数量、可售、成本和估值。新增 `primary_comparison` 逐日核原始OHLC、股数成交量、成交额及状态；按实际提交/返回序列映射订单ID，核请求量、累计成交、余量、终态、开放订单、资金冻结及同标签快照时点，并比较首次观测到的成交顺序和成交时间。JQ快照只证明截至该时点的状态，不补造撤销事件的精确时间。两端各自按原始量检查累计参与上限。

`acceptance.status` 为 `OBSERVED_FIELDS_ALIGNED`（退出码0）、`DIFFERENCES_OBSERVED`（2）或 `EVIDENCE_INSUFFICIENT`（3）；有差异且同时缺证时仍列出全部缺证。缺少必要字段不得以账户相等判通过。原九例缺少新增daily_raw采集，保留此前有限观察结论，不追补为完整六层对齐。现金残差另列，不能自动改写为股息或税。参考对齐也不证明历史状态、源端当时可得性或未观察字段已齐备。账户引用只保存在用户私有证据目录，不进入公开仓库。

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
