# Python 策略工作台

本篇汇总自定义 Python 策略运行接口（`aquant`）、单文件与多文件策略项目契约、策略工作空间（`X-Quant-Workspace`）、策略所属回测列表查询以及旧 `jqdata` 策略导入迁移工具。

## 策略编写与运行模型（`aquant`）

策略运行模块名为 `aquant`（由 A宽 worker 在回测进程内绑定，请勿安装 PyPI 上同名的第三方包），支持日频（`1d`）与分钟（`1m`）两种运行频率：

```python
from aquant import g, run_daily, attribute_history, order_target_value, set_benchmark, record

def initialize(context):
    set_benchmark("000300.XSHG")
    g.security = "000001.XSHE"
    run_daily(rebalance, time="open")

def rebalance(context):
    hist = attribute_history(g.security, 20, "1d", ["close"])
    if len(hist) == 20:
        order_target_value(g.security, context.portfolio.total_value * 0.5)
    record(equity=context.portfolio.total_value)
```

### 核心数据与交易 API

- **证券目录与订阅**：
  - `get_all_securities(['stock'], date=context.previous_date)`：返回冻结发布中在该日期有记录支持的证券表（索引为代码，未知生命周期区间记录在 `DataFrame.attrs['receipt']`）。
  - `set_universe(ids)`：动态更新交易订阅与 `context.securities`；移出订阅不影响已有持仓或挂单。
- **按需历史与当前行情读取**：
  - `history`、`attribute_history`、`get_price`、`get_bars` 直接从固定发布按需读取，无需预先将标的加入交易订阅，也无需在提交任务时声明外部证券池或固定预热天数。
  - 原价查询使用 `fq=None`；日线 `pre`/`post` 复权依赖 `ASHARE_QUANT_PYTHON_HISTORY_FACTORS` 配置的冻结因子快照，仅用于计算信号价格，公司行动分红到账仍按原生账户事件结算。
  - 日频与分钟模式均可跨频率查询已可见的 `1d` / `1m` 历史（当日完整日线 OHLCV 在 `15:00` 后可见；`get_current_data()[security].day_open` 在 `09:30` 开盘后可读）。
- **调度、持仓与基准**：
  - 支持 `before_open`（09:00）、`open`（09:30）、`after_close`（15:30）、`every_bar` 及分钟级日内定时调度。`run_daily` 的 `reference_security` 支持 `.XSHG` / `.XSHE` 沪深现货代码以绑定 A 股交易时钟。
  - `context.portfolio.positions[security]` 对已知但未持有的证券返回只读零值视图，不产生空仓记录或隐式订阅。
  - 新任务默认以沪深 300（`000300.XSHG`）为比较基准；可在 `initialize` 中调用 `set_benchmark(code)` 切换或 `set_benchmark(None)` 关闭。
- **执行模型与资金范围**：
  - 复用 RQAlpha 原生撮合、T+1、整手（100 股）、手续费与现金分红记账规则；日频订单在开盘提交撮合，分钟订单按 `next_open` 与每分钟成交量容量限制撮合。
  - 初始资金支持有限正数，上限为 100 亿元（`10,000,000,000` 元）。

---

## 本机策略存储：单文件、多文件项目与工作空间

策略文件默认存储在当前部署 `control_db` 同级目录的 `strategies/` 下（当请求携带合法的 32 位十六进制 `X-Quant-Workspace` 头时，存储在 `state/workspaces/<handle>/strategies` 下；可通过 `GET /api/v1/python/workspace-capabilities` 查看工作空间支持状态）。所有路径接口均以 `/api/v1/python` 为前缀，禁止符号链接与路径越界。

### 单文件策略接口

单文件策略以完整相对路径作为 `strategy_id`（`identity_model="saved_relative_path"`）：

| 方法与路径 | 请求参数 | 响应与说明 |
| --- | --- | --- |
| `GET /directories?path=研究` | 可选相对路径 `path`（空表示根目录） | 返回 `{root, path, entries}`，列出子目录、`.py` 单文件与 `kind="project"` 项目 |
| `POST /directories` | `{"path": "研究/日频"}` | 递归创建分类目录（权限 `0700`） |
| `GET /files?path=研究/日频/动量.py` | 必填 `.py` 相对路径 | 返回 `{path, strategy_id, source}` |
| `PUT /files` | `{"path": "...", "source": "...", "overwrite": false}` | 原子写入 UTF-8 源码（权限 `0600`）；已有文件需显式传 `overwrite=true`，否则返回 `409 STRATEGY_FILE_EXISTS` |

### 多文件策略项目接口

多文件项目是包含系统生成的 `aquant.toml`（含 `format_version = 1`、稳定身份 `project_id = "aqproj-…"` 与默认入口 `entrypoint = "main.py"`）的目录。项目改名或移动后保持同一 `project_id`（`identity_model="stable_project_id"`）。

项目内部模块通过包内相对导入（如 `from .signals import choose`）组织，每個模块均可直接 `from aquant import ...` 共享当前回测上下文：

| 方法与路径 | 请求参数 | 响应与说明 |
| --- | --- | --- |
| `GET /projects` | `?path=分类/项目` 或 `?strategy_id=aqproj-…` | 返回 `{path, strategy_id, entrypoint, files, revision}` |
| `POST /projects` | `{path, entrypoint?}` | 创建项目目录、`aquant.toml` 与初始入口文件 |
| `PUT /projects` | `{path, strategy_id, entrypoint, files, expected_revision}` | 校验 `expected_revision` 后通过操作系统目录原子交换（`renameatx_np` / `renameat2`）整包保存 |
| `POST /projects/move` | `{path, destination, strategy_id, expected_revision}` | 原子改名或跨分类移动项目目录，保持 `strategy_id` 与 `revision` 不变 |

---

## 回测提交与策略所属回测列表

### 提交回测（`POST /api/v1/python/jobs`）

1. 先调用 `GET /api/v1/python/catalogue` 获取当前 `environment_id`、支持频率（`1m`、`1d`）、`data_availability` 与资源预算。
2. 向 `POST /api/v1/python/jobs` 提交请求：
   - 单文件策略传入 `source` 与可选 `strategy_id`（相对路径）；
   - 多文件项目传入 `project={project_id, entrypoint, files}` 与相等的 `strategy_id`；
   - 必填 `environment_id`、`idempotency_key`、`name`、`frequency`、`start_date`、`end_date`、`initial_cash` 以及两项显式确认 `trusted_local_code: true`、`accept_research_model: true`。
   - 任务接受时立即将提交的源码快照冻结至任务目录，后续编辑磁盘文件不影响已提交任务。

### 查询策略所属回测列表（`GET /api/v1/python/backtests`）

```http
GET /api/v1/python/backtests?strategy_id=研究%2F动量.py&limit=25&offset=0&analysis_version=returns-v2
```

- 按 `created_at DESC, job_id DESC` 返回该 `strategy_id` 关联的全部历史与当前回测（省略 `strategy_id` 时仅列出未归属的纯草稿回测，并在 `unassigned_count` 中统计总数）。
- 每条记录包含任务状态（`status`、`display_status`、`execution_state`）、结果摘要投影 `summary` 以及单调时钟执行耗时快照 `progress.elapsed_seconds`。

---

## 已保存策略 `jqdata` → `aquant` 导入迁移

历史策略若包含 `import jqdata` 或 `from jqdata import ...`，直接提交会返回 `422 AQUANT_LEGACY_IMPORT`。可使用随包安装的 `ashare-quant-migrate-imports` 工具对 `state/strategies/` 下的已保存文件做字节级保留的 AST 导入改写（不会触碰已冻结的历史任务目录）：

```sh
# 1. 只读扫描并生成包含原文、候选文件、diff 与 SHA-256 的审核包
ashare-quant-migrate-imports prepare --state-root /path/to/state --output /path/to/review-bundle
ashare-quant-migrate-imports verify --bundle /path/to/review-bundle

# 2. 停止该部署的 API/worker 写入后，原子应用改写（或使用 rollback 回滚）
ashare-quant-migrate-imports apply --bundle /path/to/review-bundle --control-db /path/to/state/control.sqlite3 --maintenance
```

- `from jqdata import ...` 改写为 `from aquant import ...`；无别名 `import jqdata` 改写为 `import aquant as jqdata` 以保持文件内局部变量引用不变；动态导入或子模块引用标记为 `review_required` 供人工核对。
