# 回测引擎与异步任务

本篇说明通用分钟回测契约、来源行级使用规则，以及自定义 Python 与因子任务共用的异步队列、流式输入与存储预算机制。

## 通用分钟回测（`ashare_quant.backtests.minute`）

分钟回测入口为 `python -m ashare_quant.backtests.minute --help`，底层复用 RQAlpha 原生撮合、T+1、费用计算与 `execution.reservation` 挂单资金冻结。所有分钟回调均在独立的 50 位 `Decimal` 精度上下文中执行；每个交易日首根可用分钟柱出现前，账户使用当日已声明的前收盘观察值进行开盘前估值。

### 有界分钟研究输入契约（`ad-minute-research.v1`）

消费 A数达 `export_minute_research` 导出的分钟研究包时，`MinuteInputs` 与 `run_backtest` 通过三个显式开关校验准入：`accept_assumed=True`（接受模拟收盘可见时钟）、`accept_state_replay=True`（接受事后逐日状态重放）、`accept_research=True`（接受研究模型假设）。

| Profile 名称 | 窗口与标的能力 | 价格限制与公司行动模型 |
| --- | --- | --- |
| `source-labelled-closed-bars.v1`（默认） | 至多 20 只上市满 30 自然日的沪深主板非 ST、非停牌股票，至多 60 个连续交易日（首尾空仓） | 采用 `raw-previous-close-4pct-inward-cent.v1`（前收盘 ±4% 内向包络）；窗口内要求无已知公司行动重叠（`bounded_sources_no_known_overlap`） |
| `source-labelled-corporate-date-model.v1` | 可扩展至 512 个连续交易日（含前后继自然日日历 ≤740 日） | 采用 `daily-preclose-mainboard-5pct-10pct.v1`（基于源 `preclose` 的 5%/10% 研究限价模型）与 `aq.corporate-date-model.v1` 现金分红记账模型（登记日收盘确权、除息日调减成本并记应收、派息日开盘前到账） |

### 分钟行级来源准入规则（`minute-row-use.v1`）

当输入携带 A数达行级质量诊断（如 `neigezhu` 高频分钟归档）时，A宽逐行核对来源契约哈希并执行独立裁定：
- **无效或冲突价格（`price_invalid` / `price_conflict`）**：在该分钟槽拒绝撮合成交与最新估值；
- **显式不可用分钟槽（`gap_policy="explicit_unavailable"`）**：策略调用 `attribute_history`、`history`、`get_bars` 或分钟级 `get_price` 时，在已声明的缺失时间索引处返回 `NaN`（不做插值或前向填充）；撮合器在该分钟拒绝成交，账户保留持仓并将上一可用价格标记为陈旧估值（`stale`）。

---

## 共享异步任务队列（`tasks`）

自定义 Python（`/api/v1/python/jobs`）与因子计算评价（`/api/v1/factors/jobs`）共用同一控制数据库任务表、worker 租约、取消协议与存储预算。可通过 `GET /api/v1/tasks/service` 查看队列状态，契约定义见 [`contracts/tasks.openapi.json`](contracts/tasks.openapi.json)。

### 任务生命周期与恢复语义

- **幂等提交与找回**：重复提交相同 `idempotency_key` 与相同请求体直接返回原任务；网络中断或客户端刷新后可通过原幂等键或 `job_id` 查询状态与已发布结果。
- **取消与重跑**：`POST .../cancel` 将活动任务置为 `cancelling`，待 worker 停止子进程组并确认后转为最终态 `cancelled`。由于自定义 Python 内存状态不支持断点序列化，`can_resume` 固定为 `false`，调用 `POST .../resume` 返回 `PYTHON_RESTART_REQUIRED`。
- **历史兼容与升级隔离**：worker 仅执行与当前后端源码身份匹配的待处理任务；旧源码或已退役家族（`daily`、`portfolio`）的遗留未完成记录在 `display_status` 中显示为 `runtime_unavailable` 或 `retired`（计入 `historical_counts`），不会阻塞新任务队列，其已封存结果仍可通过 `/api/v1/history/jobs` 只读访问。

### 存储与产物预算

实例级存储保护通过三项指标控制（可通过 `ASHARE_QUANT_JOB_POLICY` 配置）：

| 预算项 | 默认值 | 超限错误码与说明 |
| --- | --- | --- |
| `artifact_bytes` | `512 MiB`（536,870,912 字节） | `M2_JOB_ARTIFACT_LIMIT`；限制**单个任务**保留产物的最大字节数（`artifact_budget_scope=per_task`） |
| `retained_artifact_bytes` | `512 GB`（512,000,000,000 字节） | `M2_JOB_RETENTION_LIMIT`；所有任务保留产物与待执行预留额度的总上限 |
| `disk_free_bytes` | `1 GiB`（1,073,741,824 字节） | `M2_JOB_STORAGE_RESERVE`；所在文件系统的最小空闲空间储备 |

worker 在任务启动前、运行中（每 2 秒采样）、退出后及结果发布前检查当前任务产物大小与磁盘余量；已发布结果的读取采用 64 KiB 分块流式校验输出，不受写入预算限制。

### 流式输入与实时进度采样

- **流式压缩输入（`python-demand-job.v2` / `python-project-job.v3`）**：按证券与交易日流式读取并写入 gzip JSONL（`input.json` 记录各证据文件 SHA-256 与行数），撮合缓存仅保留当前交易日，历史缓存按滑动请求窗口淘汰，事件日志保留末 500 条展示记录。
- **实时进度（`progress`）**：任务详情实时返回 worker 每 2 秒采样的单调时钟执行用时（`elapsed_seconds`）、当前阶段、已完成交易日数 / 总交易日数、模拟日期以及回测子进程的当前与峰值内存（`RSS`）。
