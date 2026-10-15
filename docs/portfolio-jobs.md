# 日频多证券任务 API v1

`0.11.0.dev21` 增加 `/api/v1/portfolio`。它调用已有 `daily_portfolio` 执行器；账户仍由 RQAlpha 6.4.1 管理。与 v6 共享 `m2_jobs` 表、幂等键、队列容量、worker 槽、租约、取消、恢复及资源预算。证券与日期来自导入数据的实际覆盖，没有 600000 或三个窗口常量限制。

dev20修正dev19的版本错绑与坏数值错误反馈：注册表行键、请求版本、冻结源码/配置、准备会话和原生收据必须相符。错绑返回409，不创建任务/账户。数值格式损坏的preflight返回`ready=false`，gaps含`security/date/field/code/value_preview`；创建返回422 `PORTFOLIO_DATA_GAPS`，不补值。金额断言显式使用局部Decimal上下文，未修改原生金额容差。

dev21补充冻结数据身份互检：请求选择/确认摘要、预检、数据描述、输入来源投影、执行范围和原生输入摘要须一致；矛盾记录在读取、恢复、执行或发布时返回409 `PORTFOLIO_FROZEN_DATA_CONFLICT`。该检查只使用已冻结记录，不重读可变目录、不重新取数，也不增加确认步骤。正常请求/结果类型及v1确认摘要算法不变。

dev22接入[正式公司行动日期模型](corporate-accounting.md)：保留完整 AD 事件与来源，持仓支持独立的股份到账和可售日；结果增加受限股、待到账股和待到账价值。旧账户这些新增字段为零，既有 v1 请求不变。来源冲突、缺登记基数和不支持事件继续阻断。

这次反例的前提是直接改写已接纳任务的SQLite与冻结文件，并同步重算相关哈希；未发现普通HTTP创建能够绕过确认。正常导入追加以内容摘要标识的数据快照，取消/恢复不改请求或manifest；并列升级归档旧任务、创建新空库，不迁移旧任务。这是内部记录的一致性检查，不是对可同时重写全部证据的本机操作者提供防篡改认证，也不代表正常回测金额失效。

完整安装包回归另发现观察候选的证券范围依赖仓库examples路径；现将同一范围随包携带，使源码与wheel行为一致。候选仍不创建账户或订单，其范围与准入规则不变。

相比dev19，路径、请求/结果类型和三个契约版本保持v1。包版本与源码身份改变，前端须更新固定身份及OpenAPI的info.version；旧控制库仍使用其原包。诊断模式的assumptions把原始导出假设放入`scope=source_provenance_only`对象的`original_assumptions`，明确其为来源记录；实际无限价/无行动规则单独列出。该模式假设摘要随之改变，须重新preflight确认，不能沿用旧确认值。

原 `/api/v1/m2` 保留 v6 路由及模型；原任务须使用其固定软件与数据目录读取。新版本不自动迁移任务，不重开旧账户。请用新任务库部署候选，保留旧包。此功能尚待独立审阅，不授予模拟盘或实盘执行权限。

## 三页工作台契约

机器契约：`GET /api/v1/portfolio/openapi.json`；仓库快照 `docs/portfolio-openapi-v1.json`。

| 页面用途 | 调用 | 必须处理 |
|---|---|---|
| 版本与算法 | `GET/POST /strategies`、`GET /strategies/{version_id}` | POST 注册现有算法的不可变版本；7种算法来自既有 signals，非任意上传代码 |
| 数据与费用 | `GET /catalogue` | 实际证券、可用交易日、来源、限制、原生环境状态、支持的费用模型 |
| 选择验证 | `POST /preflight` | 明确证券集合、起止日期、模式；`ready=false` 显示 `gaps`，不删除失败证券 |
| 创建 | `POST /jobs` | 固定版本、资金、费用、参与率、同一选择的 `assumption_ack_sha256`；稳定幂等键 |
| 列表/状态 | `GET /jobs`、`GET /jobs/{id}`、`GET /service` | 状态、完成游标、attempt、can_resume；service 的容量与计数包含 v6 和组合任务 |
| 取消/恢复 | `POST /jobs/{id}/cancel`、`POST /jobs/{id}/resume`，body `{}` | 取消后不展示成功；原账户与已提交检查点保留；成功任务不能 resume |
| 结果 | `GET /jobs/{id}/result` | `summary`、`days[].positions`、`trades`、`orders`、`decisions`、完整 `receipt`、audit、来源和经济范围 |
| 审计与日志 | `GET /jobs/{id}/manifest`、`GET /jobs/{id}/logs` | 本次输入与策略摘要、队列/worker/错误/发布事件 |

未提供代码注册 HTTP。旧单股可信 Python 契约仍由原 CLI 注册。新组合页只能宣称执行已注册的组合算法版本；代码草稿不能当作该版本实际执行的代码。任意 Python、多分钟、STAR/创业板/北交所、期货和账户复用均不在本版支持范围。

结果金额保留十进制字符串。`summary.net_return` 和 `max_drawdown` 是比例（例如 `0.10` 表示10%），基于含初始现金点的原生日终权益；不虚构基准、年化或 Sharpe，未提供项为 null。结果同时保存完整原生收据与经核对的费用和持仓，不另造撮合账本。`economic_scope` 必须跟随图表/指标显示。

`summary/account/days/trades/orders/decisions` 均有独立的类型 schema。`scope.timezone` 固定 `Asia/Shanghai`；成交、订单、决策及日终投影的 `at` 带 `+08:00`。日终是既有执行器的15:30阶段；订单时间取原生首次订单事件，缺项为 null；最终账户本身仅有日期，`account.at` 为 null。`days[].day_fee` 与 `cumulative_fee` 从原生逐笔佣金和税汇总，每日无成交费用为明确的0。原生 `receipt` 原样保留，原有无时区时钟不在证据内重写。原生浮点金额以其十进制文本输出，不冒充定点账本。

## 数据与模式

仅由本机 CLI 导入既有 `ad-daily-portfolio.v1` 真实 AD 导出，保留 public API receipts、原始文件 hash、来源与限制。HTTP 不接收服务器路径或任意数据上传。导入是固定本次运行输入的快照，不回写 AD 历史库。增加新有证数据包无需改 runner。数据是本地受信任来源声明，不代表独立认证。

- `covered_research`（默认）：需要足够行情、停牌/限价状态、行动覆盖及支付/股份日期；已有 AD 回溯状态须 `accept_retrospective_state=true` 并确认本次假设。原始来源模型保持模型标签，不宣称观察值或 PIT。AQ 临时推算限价的长窗不进入此路径。
- `price_only_no_limits`（显式）：只研究原价股票与现金，原生禁用限价验证与限价撮合，导出中的限价保持 null；不新增任何限价数值。忽略公司行动并在输入、假设、收据、audit和结果中标记“非完整经济回测/非总回报”。仍要求真实 OHLCV、停牌、120日预热与交易日，绝不补零/前填或跳过证券。

120日预热与下一开市日是现有执行器所需数据边界；主板普通A股和100股买入手数是现有原生模型边界。费用目前只提供 `fixed-commission-tax.v1`：佣金双边0.03%、最低5元，卖出税0.1%，过户/滑点0，固定跨期研究模型，非历史法定费率。`volume_ratio` 可选 (0,1] 十进制字符串，启用既有整手参与率限制。初始资金可选，账户由服务新建。

## CLI 与运行

```sh
ashare-quant --db /new/path/control.sqlite3 portfolio-data-import --input /authorized/ad-export.json --name research-window
ashare-quant --db /new/path/control.sqlite3 portfolio-catalogue
ashare-quant --db /new/path/control.sqlite3 portfolio-strategy-register --request algorithm.json
ashare-quant --db /new/path/control.sqlite3 portfolio-preflight --request selection.json
ashare-quant --db /new/path/control.sqlite3 portfolio-create --request request.json
ashare-quant --db /new/path/control.sqlite3 job-worker --once
ashare-quant --db /new/path/control.sqlite3 portfolio-result m2job-...
```

`algorithm.json` 为 `{"algorithm":"momentum"}`。`selection.json`：

```json
{
  "dataset_id": "<catalogue的64位ID>",
  "securities": ["000001.XSHE", "000651.XSHE", "600036.XSHG"],
  "start_date": "2024-01-02", "end_date": "2024-01-12",
  "mode": "covered_research", "accept_retrospective_state": true
}
```

创建请求再加入 `name`、`idempotency_key`、`strategy_version_id`、`initial_cash`、`fee_model`、`assumption_ack_sha256`。日期/证券/模式变化后必须重新 preflight。重试未知响应使用完全相同的请求和键；同键不同请求409。并发相同请求只创建一个账户。429/503含 Retry-After。GET不会启动执行器。所有读写要求本机服务身份核对；更新服务后使用匹配源码摘要/OpenAPI的新前端配置，不能把新服务地址直接替换旧 v6 固定身份。

## 验证

```sh
ASHARE_QUANT_TEST_PORTFOLIO_EXPORTS=/authorized/owner-input-final \
ASHARE_QUANT_TEST_PORTFOLIO_LONG=/authorized/scale-240.json \
PYTHONPATH=src python -B -m pytest -q tests/portfolio_jobs
```

缺外置数据明确 skip。测试覆盖真实双股、独立入口完全相等、版本/账户隔离、并发幂等、检查点提交前后强杀、成交后强杀、排队/运行/发布后取消恢复、结果篡改、未来尾部扰动和跨年。`scripts/verify_portfolio_api.py` 提供独立 HTTP/CLI、多窗口和安装包验收，输出新目录，不使用或覆盖生产账户。
