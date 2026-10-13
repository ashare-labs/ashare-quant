# 真实 TDX 分钟研究样例

两股 `600000.XSHG`、`000001.XSHE`，2026-10-09 和 2026-10-12，
每日模型时段 09:35–11:30、13:00–14:56，共 **924 条原生 1m 源记录**。
这是新命名的 `tdx-two-bank-20261009-12.v1` 研究样例；原三股两日严格题、
2024-03-04 B27 缺证及聚宽对照状态都不变。没有合成、补量或从 5m 拆出分钟。

## 运行

本地 main 整合后，已校验的离线输入和原始封存文件保存在主仓
`.verification/tdx-minute-integration/tdx-minute-research-delivery-v1/`，不进入 Git 或发布包。
在已安装原生依赖的主仓可直接运行：

```sh
cd ~/Workspace/ashare-quant
export AQ_TDX_EVIDENCE="${AQ_TDX_EVIDENCE:-$PWD/.verification/tdx-minute-integration/tdx-minute-research-delivery-v1}"
.venv/bin/python examples/tdx_minute_research/run_demo.py \
  --input "$AQ_TDX_EVIDENCE/input.json" --output .state/tdx-demo-v1 --accept-research
```

`AQ_TDX_EVIDENCE` 可以指向搬动后的同一交付目录；`sources/tdx/`、
`sources/state/binding.json`、`dist/`、`local-dependencies/` 和 `manifest.json`
一并保留即可使用下文的独立导出环境重建输入。新的输出目录由 `--output` 指定。
本次整合的冻结来源、核验摘要和日志改进 C1 见
[整合记录](../../docs/tdx-minute-integration.json)。

按主 README 安装 AQ 原生运行环境，取得下面的真实导出 `input.json` 后：

```sh
.venv/bin/python examples/tdx_minute_research/run_demo.py \
  --input /path/to/input.json --output .verification/tdx-demo --accept-research
```

这会运行两个普通 Python 文件，各新建两次原生进程，并再次进入已完成结果验证幂等。
`report.json` 给出逐笔对账和重放比较；每个运行目录保留 `strategy.py`、`input.json`、
`native-config.json`、`events.jsonl`、`result.json` 和完整性清单。
其中有订单、成交、每日持仓/现金/权益、收益，拒单原因在事件流内。
不要复用上一次失败目录；成功目录在源码、配置和输入不变时可重复读取。

也可以单独执行或修改脚本：

```sh
.venv/bin/python -m ashare_quant.minute \
  --strategy examples/tdx_minute_research/shared_cash_t1.py \
  --input /path/to/input.json --output .verification/tdx-custom \
  --config '{"initial_cash":100000,"sell_tax":0.0005}' \
  --accept-assumed-visibility --accept-state-replay --accept-research
```

费用和撮合是显式模型：双边佣金 0.0003、每单最低 5 元、卖出税 0.0005、零滑点，
单股单分钟参与上限 25%，按下一根已完成模型分钟的开盘价撮合。没有验证真实可成交性。

固定输入 SHA256 为 `8e9320af8ac8b5c51dc5ca67112199c8969fe3a34a71d1a20497d24a9d2c52b2` 时：

| 脚本 | 行为 | 成交数 | 清仓后现金/权益 | 净收益 |
|---|---|---:|---:|---:|
| `shared_cash_t1.py` | 两股共享现金；额外买单资金不足；当日卖单被 T+1 拒绝；次日卖出 | 4 | 99,818.055 | −181.945（−0.181945%） |
| `visible_momentum.py` | 从当前可见的五根分钟计算相对收益，买入一股，次日下午卖出 | 2 | 100,252.0624 | +252.0624（+0.2520624%） |

这些数值用于复现执行行为，不是策略绩效结论。原生浮点账本保留约 `1e-14` 的尾差，
独立对账容差 `1e-7` 元，不通过改账或事后调价消除尾差。

## 数据取得和离线导出

采集归 AD。使用固定 AD 提交 `171359e2b96aae6ea2fc3d45334fbb7484385c34` 的 wheel，
SHA256 `24161f86a8e6689738e5e4a89e33fffad2949f687a3acb364b80504202de8349`。
AQ 原生环境仍固定 AD `0.8.0.dev2`；请在**另一个导出环境**中安装新 AD wheel 和本 AQ wheel，
沿用 AQ 的其余锁定依赖。不要替换原生运行环境里的 AD。
`tdx_export` 会核对实际导入的 AD 全部 Python 文件与指定 wheel 一致。

2026-10-13 已封存两份原生采集，各 23,520 行（98 个源日期，2026-05-20 至 10-13）。
读取以下 manifest 的封存原文即可复现，不需要网络、浏览器或 SDK：

| 目录名 | manifest SHA256 |
|---|---|
| `600000.XSHG` | `5dd97be2c2338c5327d8aad8f29df4ed48411def5b7ab4ab0e8f2e3d41d4bab5` |
| `000001.XSHE` | `3c1f74641eb209c5d6fcf6b42b875ab8bf63f743c21f25d8c67cf3996a7bae26` |

另需 AD `minute-package-r3/binding.json` 及其原文，绑定 SHA256：
`901ac473da43d36d072424f42e0af6dc855188ee62887b4f50384ef703f16acb`。
它提供已核的日历、dated listed/suspended/is_st 源观测、实际前收，以及有界行动查询。
导出时通过 `MinuteEvidenceView` 校验全部绑定，再使用 `TDXBarView` 读取精确标签，
交叉核对前一个交易日真实 15:00 收盘。缺任意标签、缺量、前收不一致都会拒绝。

```sh
/path/to/export-env/bin/python -m ashare_quant.minute.tdx_export \
  --ad-wheel /path/to/ashare_data-0.9.1.dev13-py3-none-any.whl \
  --tdx-root /path/to/contiguous-window \
  --tdx-sha 600000.XSHG=5dd97be2c2338c5327d8aad8f29df4ed48411def5b7ab4ab0e8f2e3d41d4bab5 \
  --tdx-sha 000001.XSHE=3c1f74641eb209c5d6fcf6b42b875ab8bf63f743c21f25d8c67cf3996a7bae26 \
  --state-binding /path/to/minute-package-r3/binding.json \
  --state-sha 901ac473da43d36d072424f42e0af6dc855188ee62887b4f50384ef703f16acb \
  --output /path/to/new-input.json
```

需要自己重新采集时，使用 AD 的公开接口，明确已有 SDK 解释器及节点，不由 AQ 取数：

```python
from ashare_data import TDXBarSource

source = TDXBarSource(host="116.205.183.150:7709",
                      python_executable="/path/to/existing-eltdx-python",
                      timeout=6, min_interval=1)
for sid in ("600000.XSHG", "000001.XSHE"):
    source.fetch(sid, directory=f"/new/capture/{sid}", frequency="1m",
                 count=25600, max_pages=32, page_size=800)
```

这是现有 eltdx 3.0.3 的原生 `0x052d` 分页能力；新目录保留原始 payload、SDK 字段、
失败/空页和 manifest。节点可能滚动保留历史，未来重采不保证仍含此窗口。
`allow_partial=True` 只接受封存采集少于请求 25,600 行的事实；导出仍要求本例每条标签齐全。
源数量按 AD 解码原值使用，不舍入成交量，金额不以价格乘成交量代造。
SDK 和行情原文的既有使用范围不因 AQ 的代码许可证改变；本仓库不打包行情或 SDK。

## 必须了解的模型边界

- 原始 `bar_start/bar_end/historical_available_at` 仍为 null。新 `replay_bar_*` 独立字段将
  源标签作为结束点、前一分钟作为起点；假设收完可见，不能据此声称历史 PIT。
  新例避开首五个开盘标签和收盘竞价；没有变动旧题范围或处理旧题首条污染。
- 日状态取已封存日期观测，研究时重放到当日 09:00。真实包收件时间另存，
  不冒充盘前发布时间。ST 压缩帧完整性未知、盘中临停未知，限制保留；零量分钟拒绝。
- 交易所参考价、真实涨跌停价保留 null。原生价格校验使用围绕实际前收的
  **内收 4% 研究执行边界**，不是交易所限价。任何输入分钟高低价或限价单触及边界就拒绝，
  不模拟涨跌停排队。`high_limit/low_limit` 和范围外的 `day_open` API 明确报错。
- 假设有限行动源没有遗漏会影响这批新持仓的事件，并且本窗口无除权参考变化。
  两个完整分页分红查询及 TDX 返回行动记录的日期均不命中窗口，但仍然
  `complete=false/absence_proved=false`。任何已知登记、除权、派现、股份到账、可交易日或
  TDX 生效日在窗口内都会拒绝，不跳过行动；必须空仓开始并在 10 月 12 日覆盖内清仓。
  这不证明未报告事件不存在，也不适用于继承以前股权登记日持仓的账户。
- 日权益取 14:56 最后选择的源价格，盘后刷新到 15:30，不称为官方收盘权益。
  使用原生账本、费用、成交量限制、日单、T+1；现金、交易状态和价格风险校验不关闭。
- 只支持这里列明的两股两日研究 profile；不能任意改成其他证券/日期。
  原严格 `ad-minute-backtest.v1` 准入未放宽，JQ 服务端对照未执行。

支持的 Python/API 列表见 [分钟兼容文档](../../docs/python-minute.md)。回归命令：

```sh
AQ_TDX_RESEARCH_INPUT=/path/to/input.json .venv/bin/python -m pytest tests/minute
```

测试另用源实际成交量的 0.1% 上限重现 900/700 股分笔成交、撤余单、日末未成交单失效和午休；
故障注入验证未知事实、行动冲突、缺条、源绑定错误和窗口末未清仓均拒绝。
