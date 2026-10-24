# 因子看板与离线评价

`/api/v1/factors` 复用 AD 的聚宽命名研究库 19 项和 Alpha191 191 项。
目录说明与展示公式不参与数值计算，不承诺聚宽逐值等价，也不扩展到聚宽全库。
默认 `gtja191.explicit.v1`；保守 `gtja191.public.v1` 仍保留 8 项原文歧义。

## 来源和运行环境

安装配对的 AD wheel 后，使用 `uv sync --locked --extra native --extra factor-evaluation`。
评价 extra 引入 AD 的 Alphalens Reloaded 0.5.0 适配器；不安装 extra 时仍能浏览目录和计算因子值。
AD 固定候选同时包含 CMES 日线、冻结复权因子和评价模块。
正式部署与 worker 使用相同配置、控制库和已固定后端；预算设置变化可复用原控制库；历史保持只读可见，旧源码任务仍只能由其原运行时执行。

通过 `ASHARE_QUANT_FACTOR_SOURCES` 指定一个本机 JSON 配置文件（映射 ID 到来源）。
这是服务端登记，不接受浏览器传入任意文件路径。也会自动发现当前正式产品已绑定的日线发布。

```json
{
  "daily-snapshot": {
    "label": "已冻结日线样本",
    "kind": "market_release",
    "market_release": {
      "root": "/path/to/release-store",
      "release_id": 1,
      "manifest_sha256": "实际发布清单的64位摘要"
    },
    "cmes_factors": {
      "root": "/path/to/factor-store",
      "dataset_id": "实际CMES因子快照的64位摘要"
    }
  },
  "independent-inputs": {
    "label": "已有独立研究快照",
    "kind": "joinquant_research",
    "path": "/path/to/inputs.json",
    "sha256": "实际文件的64位摘要",
    "evidence_root": "/path/to/captured-sources"
  }
}
```

可登记 `alpha191`（`alpha191.panel.v1`）、`joinquant_panel`
（`ad.factor-panel-batch-inputs.v1`）和 `joinquant_research`
（`ad.factor-research-independent-inputs.v1`）。旧实验 schema 不被自动接受。
文件必须匹配摘要；研究快照配置 evidence_root 时同时核验原始来源文件。
输入面板必须有完整且有序的显式交易日、证券轴、布尔 membership；数值缺失为 null。

日线发布通过已有 AD reader 构造原始价量 Alpha191 输入；VWAP 仅取同一条已观测 amount / volume。
未观测价格不会前填、补零或跳过交易日。原始 CMES 不能冒充聚宽字段快照的 Sina/财报/流通股/复权输入协议。
基准、外部因子、递推初值或预热不足均按已有 AD 计算状态返回。
Alpha143 的 seed、Alpha146 的 m 要求显式数值。

## HTTP 与任务

| 方法 | 路径 | 内容 |
|---|---|---|
| GET | `/catalogue?version=gtja191.explicit.v1` | 210 项名称、分类、表达式、依赖、完整窗口、递推/排名、版本、参数与边界 |
| GET | `/sources` | 可读来源、可浏览证券、观察范围、价格口径和失败原因 |
| POST / GET | `/jobs` | 新建 / 列出因子任务 |
| GET | `/jobs/{id}`、`/jobs/{id}/result`、`/jobs/{id}/logs` | 任务、已发布结果、事件 |
| POST | `/jobs/{id}/cancel` | 使用既有队列的取消协议 |

请求固定 environment_id、幂等键、factor_id、definition、source_id、证券、history_start、
start_date、end_date、parameters 和 rank_policy。`label_end` 仅在评价时使用，必须不早于信号结束日。
证券数量和日历跨度没有独立上限；按实际输入量保留100000个证券交易日和32 MiB输入预算，并共用队列/资源保护。
登记后选定输入复制进任务目录并校验摘要；之后来源变更不会改变已接受任务。
相同幂等键和相同请求返回原任务，不会重复计算。不同请求复用键会拒绝。

独立子进程离线调用 AD，沿用现有 worker、租约、取消和原子结果发布。
完成结果可直接恢复；失败/取消/中断需要明确新建运行，不宣称递推计算有可恢复的中间检查点。
`aq.factor-exploration.v1` 返回请求、因子、来源/输入摘要、逐单元值/原始十进制文本/状态和覆盖统计。
输入或结果文件变化、源码身份不匹配都会拒绝，旧结果不会由新后端重算。

终态任务的列表、状态与日志读取复用本服务进程已完成的输入摘要校验，仅缓存文件身份、
大小、修改及变更时间等元数据，不缓存输入内容。首次读取、服务重启或文件变化重新校验；
执行前及运行中仍完整检查输入，结果文件也继续校验。缓存最多 1024 个文件校验记录，
淘汰仅导致下一次重新读取，不限制历史任务数量。此优化不改变 HTTP 响应或浏览器轮询周期。

## 评价口径

评价仅调用 `ashare_data.factors.evaluation.evaluate`，其 `ad.factor-evaluation.v1` 原样放进
结果的 evaluation 字段。Web 的同一详情页展示 IC、RankIC、独立分层收益、高低组收益差、
月度/滚动稳定性、相邻分组成员换手及成本情景。

评价参数：horizons（1–252 交易日，最多 8 项）、quantiles（2–20）、lag（至少 1）、direction（±1）、
min_assets（至少2的整数，无证券数量上限）、cost_bps（单边 bp，最多 8 项）、rolling_window。
输入 hash、固定样本标签、信号日期与价格口径由 AQ 固定，浏览器不可覆盖。
信号区间映射为其中实际交易日，历史预热不进入评价；label_end 只增加已有价格标签。

Alpha 面板须声明 `adjusted:<版本>` 的收盘价。CMES 原始价信号的评价由独立登记的
CMESFactorView 提供冻结前复权收盘价，并保留其回执；信号价格和收益价格分别披露。
聚宽命名快照使用原始 Sina close × 同快照有效复权因子，不静默改用原始收益价。
缺失价格仍缺失；缓冲不足、成员不足和分组并列各有原因。

收益是 `P[t+lag+h] / P[t+lag] - 1`，分组先按信号时点确定；完整组要求所有成员的价格路径可用。
IC 使用各观察期实际可配对成员并披露样本数。重叠持有期收益不累乘成策略净值。
固定样本、财务历史可见性和复权历史可见性不获 PIT 认证；不模拟交易约束或实际成交。
可交易策略继续使用现有 AQ 回测引擎。

## 验证与携带

```sh
python scripts/run_tests.py -q tests/factors tests/minute_tasks tests/test_database_compat.py
python scripts/generate_contracts.py
python scripts/verify_wheel.py dist/ashare_quant-0.12.0.dev20-py3-none-any.whl \
  --extra factor-evaluation --test tests/factors --output /path/to/new/verification
```

生成运行包时给 `scripts/package_runtime.py` 添加 `--extra factor-evaluation`，将评价依赖一起
导入现有 hash-locked 恢复清单。真实本机来源配置、数据库及数据不进入源码或 wheel。


Qlib Alpha158 uses namespace `qlib_alpha158` and definition `qlib.alpha158.v0.9.7`.
The unified directory contains 368 entries (19 + 191 + 158 engineered fields);
this is not a strategy efficacy count. Numerical definitions remain owned by AD.
Existing Alpha191 frozen panels and CMES released daily reads can also supply
Alpha158, with explicit source price/volume/VWAP basis in calculation provenance.
A dedicated `qlib_alpha158.panel.v1` source uses kind `qlib_alpha158`, the same
panel axes/fields/units and requires nonempty `volume_basis` and `vwap_basis`.
Dashboard panels currently use a common supplied history; per-security lifetime
ranges remain available in AD's direct API and are rejected in dashboard inputs.
No VWAP fallback is inferred by the calculator. CMES daily VWAP uses the existing
same-row amount/share-volume adapter and is missing when volume is zero.
Nonfinite Qlib results are JSON null with `QLIB_NAN` or signed infinity status;
only finite OK cells enter `ashare_data.factors.evaluation.evaluate`.
No processor fit, feature normalization beyond official formulas, or model training.

证券选择不再限制100只，评价min_assets仅要求整数且至少2；日期不再限制十年，保留日期顺序和实际来源覆盖。完整选择一次组成截面后统一计算/排名，不分小股票池分别算排名。既有100000证券交易日及32MiB输入资源预算、原生CPU/RSS/时间/文件保护不变，没有用新的证券数或年数阈值替代旧门槛。
