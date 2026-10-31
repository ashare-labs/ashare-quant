# 因子看板与离线评价

`/api/v1/factors` 提供统一因子目录浏览、多源因子值计算与 Alphalens 横截面统计评价。目录共包含 **368 项** 因子定义：聚宽命名研究因子 19 项（`joinquant`）、国泰君安 Alpha191 191 项（`alpha191`，默认 `gtja191.explicit.v1`，可选 `gtja191.public.v1`）以及 Qlib Alpha158 工程特征 158 项（`qlib_alpha158`，定义版本 `qlib.alpha158.v0.9.7`）。

## 运行环境与数据源配置

安装配对的 A数达 wheel 后，同步 `factor-evaluation` 可选依赖以启用 Alphalens 评价能力（未安装该 extra 时仍可浏览目录与计算因子值）：

```sh
uv sync --locked --extra native --extra factor-evaluation
```

通过环境变量 `ASHARE_QUANT_FACTOR_SOURCES` 指定服务端数据源登记 JSON（同时会自动发现当前部署已绑定的正式 `market_release`）：

```json
{
  "daily-snapshot": {
    "label": "已发布日线与CMES复权因子",
    "kind": "market_release",
    "market_release": {
      "root": "/path/to/release-store",
      "release_id": 1,
      "manifest_sha256": "<64位SHA256>"
    },
    "cmes_factors": {
      "root": "/path/to/factor-store",
      "dataset_id": "<64位SHA256>"
    }
  },
  "independent-inputs": {
    "label": "聚宽独立研究快照",
    "kind": "joinquant_research",
    "path": "/path/to/inputs.json",
    "sha256": "<64位SHA256>",
    "evidence_root": "/path/to/captured-sources"
  }
}
```

### 支持的数据源类型（`kind`）

| 来源类型 | Schema / 适配器 | 适用因子库与说明 |
| --- | --- | --- |
| `market_release` | `MarketRelease` + 可选 `CMESFactorView` | 供 `alpha191` 与 `qlib_alpha158` 读取已发布日线；VWAP 取同行已观测 `amount / volume`（零成交量时缺失），评价收益使用 `CMESFactorView` 提供的冻结前复权收盘价 |
| `alpha191` | `alpha191.panel.v1` | 冻结面板文件，可用于计算 `alpha191` 与 `qlib_alpha158` |
| `qlib_alpha158` | `qlib_alpha158.panel.v1` | 专用 Qlib 面板文件，要求非空的 `volume_basis` 与 `vwap_basis` |
| `joinquant_panel` | `ad.factor-panel-batch-inputs.v1` | 聚宽批量研究因子面板 |
| `joinquant_research` | `ad.factor-research-independent-inputs.v1` | 聚宽独立研究因子快照（配置 `evidence_root` 时同步核验原始来源文件） |

---

## HTTP 接口与异步任务

以下路径均以 `/api/v1/factors` 为前缀：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/catalogue?version=gtja191.explicit.v1` | 返回 368 项因子的名称、命名空间、表达式、依赖字段、预热窗口、参数与说明 |
| `GET` | `/sources` | 返回已登记数据源的就绪状态、可浏览证券列表、日期覆盖范围与价格口径 |
| `POST` / `GET` | `/jobs` | 提交新因子计算/评价任务 / 列出因子任务 |
| `GET` | `/jobs/{id}`、`/jobs/{id}/result`、`/jobs/{id}/logs` | 查询任务状态、已发布结果（`aq.factor-exploration.v1`）与事件日志 |
| `POST` | `/jobs/{id}/cancel` | 请求取消排队或运行中的因子任务 |

- **请求参数**：提交任务时指定 `environment_id`、`idempotency_key`、`factor_id`、`definition`、`source_id`、`securities`（非空唯一列表，无证券数量或日历跨度硬上限）、`history_start`、`start_date`、`end_date`、`parameters`（如 `Alpha143` 的 `seed`、`Alpha146` 的 `m`）与 `rank_policy`。
- **截面计算**：对所选证券集合一次组成完整横截面统一计算与排名；Qlib Alpha158 的非有限计算结果编码为 JSON `null` 并标注 `QLIB_NAN` 或有符号无穷状态，仅有限 `OK` 单元格进入评价阶段。

---

## 离线评价口径（`ad.factor-evaluation.v1`）

当请求包含 `evaluation` 配置与 `label_end`（不早于信号 `end_date`）时，worker 在同一任务中调用 `ashare_data.factors.evaluation.evaluate`：

- **评价参数**：`horizons`（1–252 个交易日，至多 8 项）、`quantiles`（2–20 分位）、`lag`（不少于 1 个交易日）、`direction`（`1` 或 `-1`）、`min_assets`（不少于 2 的整数）、`cost_bps`（单边基点，至多 8 项）、`rolling_window`。
- **价格与收益**：远期收益按 `P[t + lag + h] / P[t + lag] - 1` 计算；Alpha191/Alpha158 面板须声明复权价格，CMES 原始价信号由 `CMESFactorView` 提供冻结前复权收盘价计算远期收益，聚宽快照使用同快照有效复权因子。
- **评价输出**：包含 IC、RankIC、各分位数独立平均收益、多空组收益差（Spread）、月度与滚动稳定性、相邻分位数换手率及交易成本敏感性表。
