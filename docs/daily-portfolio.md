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

每次输出保留题面与源码清单、运行时版本、输入摘要、逐日信号和输入摘要、目标、订单、成交、原生账户、权益 CSV 和独立审计。原生数值字符串与展示保留不同职责：不能仅用两位小数判断一致。浮点账本核算容差固定为 `0.000001 CNY`。

公司行动使用明确的研究模型：在来源除息日确认税前应收，在来源派息日到账，股息扣税固定为零；不声称符合个人实际税率。股票变动要求入账、可交易与除权日一致，零碎股四舍五入。非相邻登记/除息日、配股、换股和其他事件拒绝进入。源覆盖不足仍为数据缺口；没有把旧历史研究假设提升成严格 PIT。原生账本与独立只读计算分别核对现金、应收、股数和权益。

同目录重新执行会校验数据、题面、策略、全部 AQ 源码及运行时身份。完整运行直接返回已提交结果；未完成运行用新的 `--invocation recover` 恢复原生 checkpoint。不同身份拒绝恢复，不能覆盖旧结果来消除失败。单目录文件锁保护写者。checkpoint 是可信本地 pickle，不接受来源不明的文件。

工程验收可离线运行：

```sh
.venv/bin/python -B scripts/verify_daily_portfolio.py --output .state/daily-portfolio-check-001
```

该脚本只生成明确标记的合成星期与价格，实际跑三策略、原样重跑、完成后重入、四个 SIGKILL 恢复点、最小佣金、共享现金拒单和未来价格扰动；合成结果不计真实策略或 JQ 验收。真正的验收还需要 AD 真实来源包、固定三窗口、旧原题回归及独立参考。

前端消费契约：显示输入来源、窗口和策略身份，以及真实/合成、历史 PIT 未认证、JQ 未采集、数据缺口；分别展示 signal.targets 和实际 positions。订单必须能追到 decision 日期、trade.order_id、费用、拒单及现金前后。权益曲线读取 equity.csv 或 result.accounts，末日与 final_account 对账；有 receivable 时独立列示。恢复运行复用同一 identity 和订单/成交 ID，不能重复累计。前端尚无本入口注册，不得用旧单股 UI 冒称支持多股。
