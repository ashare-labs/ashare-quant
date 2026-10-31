# Agent 研究 MCP

A宽在 HTTP 研究后端之上提供可选的 Model Context Protocol（MCP）薄适配层（`ashare-quant-mcp`，版本 `research-mcp.v1.1`），复用已发布的 A数达数据、输入冻结、共享异步队列、原生回测与因子评价能力。工具 schema 快照位于 [`contracts/research-mcp.tools.json`](contracts/research-mcp.tools.json)。

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

## 13 个 MCP 工具一览

| 领域 | 工具名称 | 功能说明 |
| --- | --- | --- |
| 环境发现 | `research_capabilities` | 核对产品配置、Python/因子目录、数据源与 OpenAPI 契约，返回 `configuration_id`、`environment_id` 与扩展可用状态 |
| 行情数据 | `discover_data` | 按关键词分页检索已发布证券与分钟数据源 |
| 行情数据 | `inspect_data` | 查询指定证券与日期区间的覆盖就绪状态及日线价格分页（单次上限 8 只股票 / 31 日） |
| 行情数据 | `read_minute_prices` | 单证券、有界时间窗（上限 3 日）的分钟行情分页读取，保留缺失槽与数据源标记 |
| 策略回测 | `submit_strategy` | 提交自定义日频（`1d`）或分钟（`1m`）Python 单文件源码或完整多文件 `project`，立即返回 `job_id` |
| 因子研究 | `discover_factors` | 按命名空间（`joinquant`、`alpha191`、`qlib_alpha158`）或名称分页检索 368 项因子定义與依赖 |
| 因子研究 | `factor_sources` | 查询已登记因子数据源的就绪状态、价格口径，并按 `source_id` 分页列出证券 |
| 因子研究 | `submit_factor` | 提交因子计算与可选 Alphalens 评价任务（无独立证券数或日历跨度硬上限，共用任务产物预算） |
| 任务管理 | `find_research_job` | 按原始 `idempotency_key` 找回已提交任务（支持适配进程重启后恢复查询） |
| 任务管理 | `research_job` / `cancel_research_job` | 查询 `python_daily`、`python_minute`、`factors`（及只读历史 `daily`、`portfolio`）状态 / 请求取消活动任务 |
| 日志与结果 | `research_logs` / `research_result` | 分页读取任务日志与结果（策略支持 `summary`、`trades`、`positions`、`equity`、`orders`、`strategy_logs`；因子支持 `factor_values`、`evaluation_summary`、`ic`、`rank_ic`、`quantile_returns`、`spread`、`turnover` 等分区） |

---

## 标准调用闭环与分页规则

1. **环境确认**：首次调用 `research_capabilities` 获取当前 `configuration_id` 与对应 `environment_id`。
2. **提交与幂等重试**：
   - `submit_strategy` 必须由调用方显式传入 `trusted_local_code: true` 与 `accept_research_model: true`（适配器不会默认代填）。
   - 若提交遇到网络超时或 `SUBMISSION_UNCERTAIN`，先调用 `find_research_job` 或使用**完全相同的请求与 `idempotency_key`** 重试，避免重复创建任务。
3. **有界分页读取**：
   - 单页最多返回 200 行（行数据 ≤64 KiB，单次工具响应 ≤128 KiB），后端结果读取上限为 32 MiB；通过传回 `next_cursor` 顺序翻页，游标绑定内容指纹以防漏行。
4. **契约导出与联调验证**：
   ```sh
   python scripts/export_mcp.py
   python scripts/run_tests.py -q tests/mcp
   python scripts/validate_research_mcp.py --plan PLAN.json --output NEW_DIRECTORY
   ```
