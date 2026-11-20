# Agent 研究 MCP（v2）

A宽在 HTTP 研究后端之上提供面向 AI Agent 研究工作流的 Model Context Protocol（MCP）v2 适配层（`ashare-quant-mcp`，版本 `research-mcp.v2.0`），复用已发布的 A数达数据、输入冻结、共享异步队列、原生回测、`returns-v2` 收益归因分析与因子评价能力。完整契约快照（含 Tools、Resources、Templates 与 Prompts）位于 [`contracts/research-mcp.tools.json`](contracts/research-mcp.tools.json)。

## 安装与启动

安装 `mcp` 可选依赖（基于官方 Python SDK `mcp==2.3.0`）：

```sh
uv sync --locked --extra native --extra factor-evaluation --extra mcp
```

先按同一份 `deployment.json` 启动研究 API（如 `http://127.0.0.1:18839`）与 `task-worker`，再配置或启动 MCP 适配器：

```sh
# 1. 作为本地 MCP Host（如 Claude Desktop / Codex / Cursor）的 stdio 子进程启动
.venv/bin/ashare-quant-mcp --backend-url http://127.0.0.1:18839

# 2. 本地 Streamable HTTP 调试模式（绑定 http://127.0.0.1:8766/mcp）
.venv/bin/ashare-quant-mcp --backend-url http://127.0.0.1:18839 \
  --transport streamable-http --port 8766
```

- `--backend-url` 仅接受带端口的本机回环地址（`127.0.0.1` 或 `::1`），适配器固定绑定本地 loopback。

---

## 12 个面向任务的 MCP 工具

| 领域 | 工具名称 | 功能说明 |
| --- | --- | --- |
| 环境发现 | `get_environment` | 获取精简的就绪状态、可用交易日历区间、支持频率（`1m`、`1d`）、默认基准、分钟源、因子源及限额（传 `detail=True` 展开完整目录元数据） |
| 行情数据 | `search_securities` | 按股票代码或名称分页检索已发布证券（自动兼容 `.SZ`/`.SH`/`sz`/`sh`/6 位数字写法）并列出分钟数据源 |
| 行情数据 | `get_prices` | 统一日线（`1d`，单次 ≤8 只 / ≤31 日）与分钟（`1m`，单证券 / ≤3 日）行情读取；自动归一化代码、自动补全分钟 `+08:00` 时区，支持 `fields` 列裁剪与游标分页 |
| 因子研究 | `discover_factors` | 按命名空间（`joinquant`、`alpha191`、`qlib_alpha158`）或关键词检索 368 项因子定义并附带已注册因子数据源列表；传入 `source_id` 时分页列出该源下的证券 |
| 策略预检 | `check_strategy` | **不入队**，秒级完成 Python 语法、`initialize(context)` 入口、`aquant` 符号白名单与禁用聚宽函数、股票代码字面量及日期区间预检 |
| 策略回测 | `run_backtest` | 扁平参数提交日频（`1d`）或分钟（`1m`）回测（支持内联 `source`、`project` 或工作台 `strategy_ref` 及 `workspace`），缺省自动生成确定性幂等键，默认等待完成（`wait_seconds=30`）并直接返回含 20 项 `returns-v2` 指标的运行视图 |
| 因子研究 | `run_factor` | 扁平参数提交因子计算与可选 Alphalens 评价（自动匹配因子简名、默认定义、就绪 `source_id` 与全证券池 `"all"`），默认等待完成并返回评价摘要 |
| 结果与诊断 | `get_run` | 按 `run_id` 或 `idempotency_key` 查询统一运行状态、进度、成功后的 `returns-v2` 20 项核心指标/因子摘要，或失败后的脱敏用户栈帧与最后 50 条策略日志（无需 `family` 参数） |
| 明细分页 | `get_run_details` | 按 `run_id` 分页读取 `trades`、`positions`、`equity`、`orders`、`strategy_logs`（失败任务亦可读取）、`analysis_series`、`drawdowns`、`monthly`、`rolling`、`factor_values`、`ic`、`rank_ic`、`quantile_returns`、`system_logs` 等分区 |
| 实验管理 | `list_runs` | 列出近期回测或因子实验（支持按 `kind`、`strategy_id`、`state` 筛选） |
| 实验管理 | `compare_runs` | 横向对比 2–10 个实验的参数差异（`varying_parameters`）与核心绩效指标 / 因子 IC 指标 |
| 任务控制 | `cancel_run` | 按 `run_id` 请求取消排队或运行中的回测/因子任务 |

---

## 内置 MCP Resources 与 Prompts

| 类型 | 标识 / URI | 说明 |
| --- | --- | --- |
| Resource | `aquant://api-reference` | `aquant` 全部可用函数签名、`context`/`portfolio`/`Position` 属性速查表及不支持的聚宽函数替代说明 |
| Resource | `aquant://model-notes` | 日频/分钟撮合规则、`fq` 复权说明、A 股 T+1 与整手限制、数据来源边界说明 |
| Resource | `aquant://templates/{name}` | 开箱即用的标准策略模板：`daily_rebalance`（日频调仓）、`minute_timing`（分钟择时）、`multi_file_project`（多文件项目） |
| Prompt | `research_loop` | 标准量化研究闭环引导（查环境/标的 → `check_strategy` 预检 → `run_backtest` 运行并等待 → `compare_runs` 横向对比） |
| Prompt | `port_joinquant_strategy` | 将聚宽 `jqdata` 策略迁移为 `aquant` 并通过 `check_strategy` 校验的提示模板 |

---

## 标准调用闭环

1. **查阅规范与预检**：读取 `aquant://api-reference` 或模板，调用 `check_strategy(source=..., start_date=..., end_date=...)` 在入队前排除语法与 API 错误。
2. **一键提交并获取指标**：调用 `run_backtest(source=..., start_date=..., end_date=..., securities=['000001.SZ'], wait_seconds=30)`，无需传递环境哈希或确认标志；任务完成后直接在响应的 `metrics` 字段拿到夏普比率、最大回撤、年化收益、超额收益等 20 项 `returns-v2` 指标。
3. **参数迭代与横向对比**：修改 `parameters={...}` 再次调用 `run_backtest`（自动按参数内容生成新幂等键），随后调用 `compare_runs(run_ids=[id1, id2])` 对比不同参数下的指标表现。
4. **契约导出与联调验证**：
   ```sh
   python scripts/export_mcp.py
   python scripts/run_tests.py -q tests/mcp
   python scripts/validate_research_mcp.py --plan PLAN.json --output NEW_DIRECTORY
   ```
