# 研究任务

需要 README 中的固定 A数达依赖、可选 native 引擎以及有权使用的固定数据产品。A宽消费公开数据接口，不采集、补齐行情或维护第二套行情数据库。质量、来源、可见性和缺项按 owner 返回保留。

## 数据与窗口

先通过 A数达完成 import、validate、compose、export，满足合同的导出才能使用 `job-data-import --input` 导入。配置 `ASHARE_QUANT_M2_STORE` 后读取 `job-catalogue`，使用自己的摘要。

| 窗口 | 既有执行范围 |
| --- | --- |
| m2a | 2020-01-03、2020-01-06 |
| w1 | 2020-01-03 至 2020-01-16 的十个交易日 |
| w2 | 2020-01-06 至 2020-01-17 的十个交易日 |

服务限 `600000.XSHG`、日频、现货多头、空仓初始化、非负整百股目标（模板买入至少 100 股）。输入前一交易日收盘，按声明的当日事后收盘模型执行；固定历史费用模型不是当前市场费率建议。

`end_date` 必须来自窗口的 `scope.trading_dates`，执行原首日至该日的连续前缀；省略或 null 表示完整窗口。不接受任意 start_date、跳日或拼窗。未准入日期返回 `M2_END_DATE_NOT_ADMITTED`，不会自动取整，首日结束也不会强制平仓。

## 创建和执行

按[策略编写](strategies.md)注册后，填写自己的 request.json：

```json
{
  "window_id": "m2a",
  "end_date": "2020-01-03",
  "name": "首日买入持有",
  "idempotency_key": "python-hold-first",
  "data_binding_sha256": "从自己的catalogue复制64位摘要",
  "assumption_ack_sha256": "从所选窗口复制64位摘要",
  "parameters": {
    "template": "registered_python.v1",
    "strategy_version_id": "从注册结果复制64位摘要",
    "initial_cash": "10000",
    "strategy_parameters": {}
  }
}
```

占位文本必须替换后才能提交。假设摘要表示接受所列研究模型，不代表未知事实已验证。

```sh
.venv/bin/ashare-quant --db .state/research.sqlite3 job-create --request request.json
.venv/bin/ashare-quant --db .state/research.sqlite3 job-worker --once
.venv/bin/ashare-quant --db .state/research.sqlite3 job-result JOB_ID
```

将 JOB_ID 换成创建结果。job-status、job-manifest、job-logs、job-cancel、job-resume 同样接受任务 ID。同幂等键改变请求会拒绝，恢复不会延长已冻结日期。RQAlpha 是账户唯一真值。

HTTP 任务入口为 `/api/v1/m2/jobs`，任务 OpenAPI 为 `/api/v1/m2/openapi.json`，全量文档为 `/api/v1/docs`。合成演示的 `/api/v1/runs` 与真实研究任务是不同入口。

并发预算、队列满处理、故障恢复与保留历史的升级操作见[任务服务可靠性](task-service.md)。

## 数据库、恢复和搬迁

任务清单为 `m2-job-manifest.v6`。SQLite 表结构版本不足以判断任务兼容性。旧 v3/v4/v5 任务库会在初始化写入前拒绝，HTTP 409 / CLI 2，错误 `M2_DATABASE_VERSION_INCOMPATIBLE`。保留旧库与同名 `.m2-jobs` 目录，用原固定后端读取；为当前版本选新控制库。同为 v6 也不代表源码、依赖和任务身份可互换。

迁址前停止服务、worker 与原生子进程，一起复制控制库、同名 `.m2-jobs` 目录及存在的 WAL/SHM。使用相同源码、依赖和数据内容，重新配置 Store 路径。不要在两个位置同时执行同一账户，本地锁不协调多机器。

恢复核对冻结源码、参数、读取证据、检查点和提交记录，不重跑已提交的 Python 决策。损坏或不兼容时保留原文件和失败日志，不绕过摘要检查。

## 固定实验入口

`python -m ashare_quant.execution_six --help` 和 `ashare_quant.round_one` 各模块有固定实验驱动，需要额外 reader、绑定及合法数据。各自固定 owner 版本，不能用最新 A数达整体覆盖；它们不属于通用日频服务。

稀疏分钟、事后容量、点价、跨日和公司行动研究均带模型限制，见[能力边界](compatibility.md)。公开包不提供实验私有输入、原始参考轨迹或浏览器材料。
