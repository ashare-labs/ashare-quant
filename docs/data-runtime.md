# 数据、启动与恢复

本篇说明 `ashare-deployment.v1` 部署清单的配置方式、服务启动、运行状态与源资产备份恢复，以及 `/api/v1/product` 与 `/api/v1/market` 的数据查询接口。

## 部署清单配置（`deployment.json`）

所有服务入口按以下优先级解析部署清单：
1. 命令行 `--deployment`；
2. 环境变量 `ASHARE_DEPLOYMENT`；
3. `ASHARE_DATA_ROOT/deployment.json`；
4. 用户默认配置 `$XDG_CONFIG_HOME/ashare/deployment.json`（默认 `~/.config/ashare/deployment.json`）。

清单中的相对路径以 `data_root` 为基准（`data_root` 相对清单文件所在目录解析）：

```json
{
  "schema": "ashare-deployment.v1",
  "data_root": ".",
  "assets": {
    "main": {"kind": "local_daily", "path": "/data/ashare-data-local", "role": "market", "retention": "preserve"},
    "releases": {"kind": "store", "path": "ad/releases", "role": "publication", "retention": "preserve"},
    "released-market": {"kind": "market_release", "path": "ad/market-releases", "role": "market", "retention": "preserve"}
  },
  "profiles": {
    "product": {
      "kind": "product",
      "daily_asset": "main",
      "release_store_asset": "releases",
      "market_release": {"asset": "released-market", "release_id": 1, "manifest_sha256": "<64位SHA256>"},
      "control_db": "aq/product/control.sqlite3",
      "paper_root": "aq/product/paper",
      "api_port": 18839,
      "paper_port": 18818,
      "web_port": 41839,
      "default_dataset_id": null,
      "default_reference_set": "released-reference",
      "reference_sets": {
        "released-reference": {"kind": "market_release"}
      }
    }
  }
}
```

- **参考数据组（`reference_sets`）**：推荐使用 `"kind": "market_release"` 直接复用 `market_release` 中的逐日状态、涨跌停限价与公司行动；也支持登记独立的 `daily_execution` 或 `historical_market` 绑定资产。
- **可选比较基准发布（`benchmark_release`）**：若主交易发布不含指数行情，可在 profile 中附加 `benchmark_release: {"asset": "...", "release_id": 1, "manifest_sha256": "..."}` 供回测收益比较只读查询。

---

## 检查与启动服务

```sh
# 1. 只读诊断数据库结构、AD 身份、原生依赖与资产完备性
python -m ashare_quant.deployment --deployment /data/product/deployment.json doctor

# 2. 初始化控制数据库与候选状态目录
python -m ashare_quant.deployment --deployment /data/product/deployment.json init

# 3. 输出配对前端环境变量（FRONTEND_PORT、ASHARE_API_URL、ASHARE_PAPER_URL）
python -m ashare_quant.deployment --deployment /data/product/deployment.json commands
```

在各自终端前台启动三个服务进程：

```sh
python -m ashare_quant.deployment --deployment /data/product/deployment.json api
python -m ashare_quant.deployment --deployment /data/product/deployment.json task-worker
python -m ashare_quant.deployment --deployment /data/product/deployment.json paper
```

- 统一行情入口也可通过 `python -I -B -m ashare_quant.deployment.market_entry --root <entry-root>` 启动；若目录 `catalog.json` 配置了 `account_reader`，可经只读子进程查询已迁移的独立账户。
- 当发布存储遇到短暂 `SQLITE_BUSY` 锁竞争时，发布接口返回 `503 PRODUCT_DATA_UNAVAILABLE`（附 `Retry-After: 2`）；客户端等待至少 2 秒后以相同参数重试即可，相同源字节与参考绑定会幂等复用同一数据集身份。

---

## 备份与空目录恢复

### 备份运行状态与可选源资产

停止当前配置的 `api`、`task-worker` 与 `paper` 后执行备份：

```sh
# 备份 AQ 运行状态（控制库快照、任务产物、检查点、paper 账本、已发布 Store 与必需小型资产）
python -m ashare_quant.deployment --deployment /data/product/deployment.json backup /backup/product-state
python -m ashare_quant.deployment verify /backup/product-state --expected-sha256 <MANIFEST_SHA256>

# 显式包含未受保护的大容量源资产（如 LocalDaily 主库）
python -m ashare_quant.deployment --deployment /data/product/deployment.json backup /backup/product-full --include-source-assets
```

### 在空目录恢复

先使用交付包中的 `rebuild_runtime.py` 重建匹配的虚拟环境，再校验并恢复状态：

```sh
python /delivery/runtime/rebuild_runtime.py /restore/venv --bundle /delivery/runtime --offline
/restore/venv/bin/python -m ashare_quant.deployment restore /backup/product-state /restore/product --expected-sha256 <MANIFEST_SHA256>
/restore/venv/bin/python -m ashare_quant.deployment --deployment /restore/product/deployment.json doctor
```

---

## 产品数据、财务与分钟查询接口

### 正式数据与历史任务查询

| 路径前缀 | 功能说明 |
| --- | --- |
| `GET /api/v1/product/config`、`catalogue`、`prices`、`coverage` | 查询当前配置身份（`configuration_id`）、证券目录、日线行情与参考覆盖 |
| `POST /api/v1/product/releases` | 将通过覆盖检查（`covered_research` 或 `price_only_no_limits` 诊断模式）的证券与日期范围发布为冻结研究数据集 |
| `GET /api/v1/market/stores`、`/market/{alias}/{dataset}/...` | 通用只读 Store 行情与覆盖查询（对应 CLI `market-describe`、`market-read`、`market-coverage`） |
| `GET /api/v1/history/jobs`、`/history/strategies` | 只读查询 `daily`、`portfolio`、`python`、`factors` 历史任务详情、manifest、日志、结果与历史注册策略 |

### 财务质量查询（`/api/v1/product/financial/*`）

- **`POST /api/v1/product/financial/point`**：按 `security`、`field`、`period`、`source`、带时区的 `as_of` 及 `mode`（默认 `official_daily`，可选 `historical_pit` 或 `source_research`；查询原生表行时附 `statement` 与 `title`）查询单点财务数据。
- **`GET /api/v1/product/financial/coverage`**：接受可选 `security` 与 `day` 参数，返回分类覆盖状态（区分正常覆盖、已退役交易代码、跨源别名、停牌日、退市与未解决缺口）。

### 分钟数据查询（`/api/v1/product/minute/*`）

委托给 A数达 `LocalMinuteProduct` 执行：
- **`GET /api/v1/product/minute/sources`**：返回已配置的分钟来源 ID（默认 `tdx`，支持 `canonical`、`free_stockdb`、`neigezhu`）、资产与证据绑定及覆盖范围。
- **`POST /api/v1/product/minute/prices`**：接受单只证券、带时区且精确到整分钟的闭区间 `start`/`end` 与可选 `source`（`auto` 选择默认来源），原样返回 `source`、`binding`、`rows` 与质量诊断 `report`。运行期若部署配置或资产路径发生变更，接口返回 `409 PRODUCT_CONFIGURATION_CHANGED` 并要求重启服务。
