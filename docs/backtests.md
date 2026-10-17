# 分钟与合成回测

分钟入口 `python -m ashare_quant.backtests.minute --help`；组合入口 `python -m ashare_quant.portfolio --help`。输入保留原始来源、时间语义、零量记录、预热及执行域；研究导出不能变成实时观察证据。

examples/minute/make_fixture.py 生成明确合成的最小分钟输入；equal_weight.py、limit_and_exit.py、rotation.py 是可信示例。examples/minute_research 是可导入的 Python 示例包。对照验证器位于 tests fixtures 或 scripts，产品不导入它们。

分钟运行保留原生撮合、T+1、费用、共享现金与预算预留。预算在 execution.reservation，根据未成交量及已发生费用冻结资金，不重复实现成交账本。检查点、隔离和 RQAlpha 内容验证在 execution.rqalpha。

冻结午休成交容量案例的复验入口是 `python -m tests.fixtures.minute_reference.audit_capacity_regression --help`。它核对外部案例、策略、输入和原生结果哈希，在新目录重跑并审计分钟容量、现金差额及成交变化。该入口限定既有 `R09-lunch-participation` 案例；历史 ID、`aq-minute-r09-capacity-fix.v1` schema 和证据哈希保留，名称不表示支持任意案例。所需历史输入不随包发布；案例和基线只读，结果只写显式指定的新目录。

`ashare-quant demo --engine fixture-replay-v1` 是确定性合成演示；native extra 可选 `--engine rqalpha`。这些场景验证控制层和原生约束，不代表真实行情回测。输出必须写新目录。
