# 日频连续模拟基础：paper-api.v1

此实现基于 AQ `b6728f93c30a578ba5818a4548b63b21449182bb`，仅开放合成输入的原生连续账户验收。`observed_data_enabled=false`、`continuous_paper_accepted=false` 是可查询的能力边界。真实策略、AD 执行数据及 quant-web 联验尚未完成；没有自动采数、系统定时器、券商或部署入口。

共同真实题面为父任务维护的 `real-strategies/manifest.json`：八股、2020/2022/2024 三窗，等权、动量和趋势三策略。此模块不另选策略或窗口。撮合时间已对齐为前收信息于 D 日 09:00 冻结目标、09:30 原始开盘价成交、15:30 原始收盘价估值。借用 RQAlpha 的 OPEN_AUCTION 事件执行日频开盘研究模型，不主张真实集合竞价队列或流动性。

## 可复用实现

| 部分 | 复用及补充 |
| --- | --- |
| 单账本 | RQAlpha 6.4.1 的 Portfolio、Account、Position、Broker、Trade、费用与 T+1。SQL 不存第二份现金或持仓账本。 |
| 预算冻结 | 继承已有 B44 `BudgetAccount`，新类显式保存原生账户及订单/成交身份映射；旧类及旧恢复限制不变。 |
| 原子状态 | 复用 `rqprobe.checkpoint.AtomicFile/StrictHelper`，一起保存原生账户、挂单、事件游标、订单/成交序号、费用累计、行情标记和结算游标。 |
| 跨日 | 每日一个独立原生进程，先恢复上一日完整状态，再只输入新一天；无逐日新建空仓后回放历史。 |
| 控制状态 | 复用 M2 原子文件、锁和摘要函数；SQLite 保存冻结意图、状态、租约、防陈旧写入令牌和告警。 |
| 人类报告 | 已提交原生收据、现金/持仓/NAV 对账、订单与成交、目标差额和中文摘要；异常有稳定错误码和后续动作。 |

## 最小契约

`SessionConfig` 冻结账户、策略内容摘要、共同 manifest 摘要、证券全集、初始资金、费用、独立风控、AQ 源码和运行时指纹。修改任一项必须明确创建新会话，不能借版本更新绕过同一会话的日意图槽位。一个账户的运行目录必须固定；目录迁移/多机复制不是已验收功能。

`Decision` 提供 D/P/下一开市日、09:00 逻辑时刻、收到时刻、09:05 截止、策略收据与数据 manifest 摘要、全证券前收/状态/规则/覆盖证据，以及每只证券明确的目标股数。`expected_parent` 必须等于上一日已提交检查点摘要。所有日期必须来自上游权威日历；本层只校验链连续性，不伪造权威日历。

`CloseInput` 在数据完整到达后封存 D 日原始 OHLCV、收到时刻/截止、水位和冲突标识，并绑定唯一意图。内部受控历史演练先收齐当日输入，再按冻结的逻辑时间推进事件；**这不等于当天 09:30 已在线接收开盘行情**。真实连续调度需 AD 分时角色接口及新鲜度门禁就绪后单独接入。

每日原生事件为 BEFORE_TRADING 09:00、OPEN_AUCTION 09:30、估值 BAR 15:30、AFTER_TRADING 15:30；日结只一次。只在开盘提交一次，先卖后买，同侧按证券排序。未成交订单在估值 BAR 前经原生撤单，避免收盘误成交；翌日只按新策略意图重算，不能自动重复旧目标。

失败停在当前冻结日：缺行、过期/冲突水位、未知停牌/ST/涨跌停规则、未知公司行动覆盖、截止超时、风控失败、失锁、状态或报告损坏均不推进 head。当前仅支持明确无公司行动的合成样例；未知不能当无行动，停牌不能变成可交易，缺价格不能填零。

独立风控默认保守：集中度、总敞口、持股数、单笔额、换手、整手、T+1、现金和费用余量。合成默认上限并非共同场景 98% 权重的正式风控配置；当前以涨跌停上限和调仓前持仓做保守预算，不预支未成交卖单收入。真实接入必须由父任务统一冻结策略与风控的相容参数和逐单准入语义，不能为通过测试暗中放宽。

## 幂等、恢复和验证边界

唯一槽位是 `(session, exchange_day)`；相同 JSON 语义返回原意图，不同内容 409。同账户文件锁覆盖控制进程与原生子进程，租约过期不能越过仍存活的锁。父进程死亡时子进程守卫退出。恢复必须显式 `resume`，核对当前 parent 和冻结输入；不能用改写的策略/数据继续旧状态。

完整事件和游标一起原子替换检查点。事件中途崩溃回到前一完整事件；落盘后崩溃从下一个事件继续。完整日检查点已落盘但报告/SQL 未发布时，只校验并发布已有收据，不再调用原生撮合。对账核验原生现金、持仓、可卖数量、成交 ID 唯一性和 NAV；已提交文件摘要变化必须显示 `evidence_conflict`。

检查点使用现有原生 pickle/jsonpickle，仅允许本机本运行目录生产的可信文件，HTTP 不接收检查点。源码/依赖或引擎指纹变化即拒绝旧会话续跑；没有自动状态迁移。部分成交流动性模型、公司行动应收与跨日到账、长期状态压缩、多机调度未被此版本开放。

## 后端接口与责任

独立 FastAPI app：`ashare_quant.paper.api.create_app(root)`。所有接口 `Cache-Control: no-store`，业务失败返回 `{error:{code,message,retryable,next_action}}`。请求模型定义在 `contracts.py`，可通过 app 的 OpenAPI 获得完整字段。

| 方法/路径（前缀 `/api/v1/paper`） | 结果 |
| --- | --- |
| GET `/capabilities` | 可用模式、未验能力、待接依赖 |
| POST `/sessions` | 创建/复用冻结会话 |
| GET `/sessions/{id}` | 当前 head、逐日状态、告警与证据冲突 |
| POST `/sessions/{id}/decisions` | 冻结日意图 |
| POST `/sessions/{id}/closes` | 封存执行数据，转 ready |
| GET `/sessions/{id}/days/{day}/report` | 仅已验证、已提交的日报 |
| POST `/sessions/{id}/pause`、`/resume` | 要求 reason 和 expected_parent 的控制动作 |

本地显式 worker：`python -m ashare_quant.paper --root <固定运行目录> work --session <id> --day YYYY-MM-DD`。同一 CLI 提供 create、prepare、seal-close、status、report、pause/resume。`serve` 仅绑定 127.0.0.1；本阶段未启动或安装任何常驻服务。

AD 负责权威日历、原始价格、状态/规则、公司行动、来源/覆盖/水位与不可变执行导出，并明确数据缺口。AQ 接口消费证据，负责逻辑时钟、策略收据绑定、独立风控、唯一原生账本与恢复。quant-web 在独立仓库负责人类入口：显示未验能力、风险失败、目标与实际持仓差额、暂停/恢复理由及坏证据；AQ 不包含前端。

## 真实联验前必须接续

1. AD 固定可信执行包与 schema/reader 版本、逐字段来源与覆盖，以及日增量“取新数据/开盘/收盘”角色；现有 research preflight 不构成 execution 授权。
2. 三种真实策略逐日冻结收据接口：代码/参数/manifest/hash、信息截止、数据依赖、目标与调仓原因、前一权益。共同策略文件已发布，不能另造一套策略入口。
3. 公司行动延续：现金应收/到账、股份变化、交易限制及恢复状态须使用原生能力补验；第一批无行动合成测试不能替代这些样本。
4. 保留 2020 原样首跑及失败，再做 2022/2024 留出多股多窗联验；比较完整运行与分日/重启运行的订单、费用、现金、持仓、权益和拒单，不按收益选样。
5. 真实场景通过后再接外部日频调度与 quant-web 人类验收，由父任务协调本地 main 集成；自动采数、真实连续验收和总目标当前仍未完成。

离线复现：使用固定 native 环境，从仓库执行 `PYTHONPATH=src python -B -m pytest -q -p no:cacheprovider tests/paper tests/second_batch/test_budget_account.py tests/round_one/test_reservation_account.py --basetemp=<新的证据目录>`。`tests/paper/fixtures.py` 是明确合成的样例输入生成器，`examples/daily-paper-session.json` 可独立创建合成会话。故障注入只杀本测试创建的进程。
