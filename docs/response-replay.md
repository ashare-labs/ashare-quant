# 真实响应回放回归

入口是 `scripts/replay_sealed_responses.py`。它封存既有响应、请求/收讫时间和顺序，再调用生产路径：

`A数达 capture → read/assess → A宽 prepare/deliver → OnlineService.advance → RQAlpha native Account`

只替换传输与时钟；不另造撮合、费用或账户引擎。主进程与既有 native 子进程均禁止联网。默认运行先验证 14/15 日原始结果，然后执行派生故障和原生账户回归。

在 A宽工作树运行（使用已配置 native 依赖的 Python，无需安装）：

```sh
/Users/yinming/Workspace/ashare-quant/.venv/bin/python -B scripts/replay_sealed_responses.py \
  --evidence-root /Users/yinming/Documents/Codex/2026-10-12/task-2 \
  --ad-source ../ad-replay/src \
  --output ../response-replay-result
```

输出目录必须不存在。`corpus/manifest.json` 绑定原文件 SHA、每次请求的 URL、收讫时刻、顺序、状态和原文；`corpus/blobs` 保留原文。日线的历史比较依赖递归封存。再次运行只需把 `--evidence-root ...` 换为 `--corpus ../response-replay-result/corpus`，并指定新的输出目录。运行中不读取当前账户检查点，更不重建或迁移当前账户。

`report.json` 给出基线重算、来源分类、原生账户比较及反例结果。原始文件和派生文件分开保存；每个派生包记录母输入、变更说明及 corpus SHA。回放新建 `replay-only-*` 身份，决策/成交证据是 `sealed_response_replay`，报告 `real_calendar_observation=false`。普通采集读端拒绝回放包。

| 覆盖 | 实际路径与判据 |
| --- | --- |
| 14 日成功 | 真实 admission、首次 execution、收盘日线；三笔成交、费用 15、现金 13436、收盘 NAV 29988 与封存逐项一致 |
| 部分成交/取消余量 | 600000 成交 300，余量 700 被取消；后续真实分钟包被幂等冲突拒绝，不能补成交 |
| 15 日失败与恢复 | 三次准入失败、第四次成功；execution-01 的原始 TypeError 可复现，修复后保留数据源阻断；execution-02 通过，原生接续无新成交，T+1 可卖数量为 300/200/100 |
| 时钟与跨日 | 旧日期快照、旧分钟、错误 HHMMSS 单位、收讫早于请求、请求与前一收讫交叠都阻断；UTC/+08 等价时刻通过实际准入 |
| 缺字段/部分返回 | 缺失快照列、分钟 volume、列表总数不完整和分数股数被拒绝，失败回执不能写入账户 |
| 中断/429/503 | 原始 SSL 失败及后续恢复保留；派生连接中断、429、503、分钟 503 经同一采集和准入路径阻断，恢复的干净记录可继续执行 |
| 股/手单位 | 原始成交量按股消费；显式把两次分钟响应除以 100 后，600000 不足一手而零成交。反例仅证明错误换算的后果，不声称源使用手 |
| 重复/进程恢复 | prepare、deliver、advance 重复不改状态和检查点；派生原生进程交易中断后 resume，现金、费用、成交、取消余量与正常路径一致 |
| 封存完整性 | 改原文或改 envelope 完成时刻均在账户创建之前失败；两次独立运行 report 字节一致 |

9 组原始采集记录、16 组明确标注的派生输入；另执行原生恢复、重复投递和新分钟冲突。故障按 `source_unavailable`、`source_contract_blocked`、`invalid_receipt_timeline`、`program_error` 分类，原始 TypeError 在 `originals` 中单独保存。

本次定向修复：

- AD v2 在上游准入已有 blockers 时不再读取不存在的 `sse_snapshot` 或转换空限价；缺失新浪报价不会再引出三个 TypeError；报价/分钟源中断有明确错误码。历史 v1 按旧规则重算，因此原始失败 SHA 和 assessment 不会被“修好”。
- SSE 时间诊断分别报告跨日与超前，带来源 SHA、原收讫和精确微秒差。三份真实失败仍是 64712、74989、108526 微秒超前，阻断未放宽。证据不能确定是源时钟、秒标签取整还是本机时钟，不推断原因。
- AD v2 读端核验串行请求顺序。旧规则可接受请求交叠的派生反例，新规则拒绝。
- AQ 以带时区 datetime 比较 `not_before` 和计划起点，修复等价 UTC/+08 字符串不等造成的错误拒绝。

`tests/paper/test_response_replay.py` 使用明确提供的封存语料运行两遍完整入口，并验证篡改拒绝：

```sh
ASHARE_RESPONSE_CORPUS=/path/to/corpus ASHARE_CALENDAR_AD_SOURCE=/path/to/ad-replay/src \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /path/to/native-python -B -m pytest tests/paper/test_response_replay.py -q
```

限制：execution-01 在生成 packet 前崩溃，只有逐源原文、请求/收讫和 attempt.finished_at；回放明确使用后者作为结束边界，不能冒称恢复了不存在的 packet.completed_at。封存中没有真实 429/503 或进程崩溃，相关项均为派生故障。行情可用性、源时钟原因和下一交易日真实运行仍需各自证据；本结果不是新增真实市场结果，也不改变当前固定包 `cc61cae`。AD/AQ 候选需成对合并，不能单独将 AD v2 投入只认识 v1 的旧 AQ。
