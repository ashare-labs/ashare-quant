# Python 策略工作台

网页 `#/workbench` 是自定义 Python 日频 / 分钟研究入口。导入或编写 `.py`，选择频率，配置股票池、日期、资金和 JSON 参数，确认可信代码与回溯模型后选择“准备数据并回测”。系统从当前正式发布冻结来源、参考资料和策略；不要求手动发布数据。`#/portfolio` 保留七种日频算法示例，固定窗口的受限 `on_decision` 仍在原任务入口。

策略定义 `initialize(context)`；可选 `before_trading_start`、`handle_data`、`after_trading_end` 和 `run_daily`。`context.parameters` 返回参数副本，`context.securities` 是本次固定股票池；数据查询仅访问已准备股票及已可见历史。原价历史查询需显式 `fq=None`。已有 jqdata 子集提供 history、attribute_history、get_price、get_bars、get_current_data、委托/撤单、record 和 log。没有全市场动态扫描、财务查询或任意频率能力。

日频提交 `frequency="1d"` 和 `warmup_sessions`（默认 20，1—120）。最多 8 股、126 个交易日、186 个自然日；预热只供查询，不生成交易。日频只支持沪深普通主板，上市至少 30 个自然日。`run_daily` 支持 before_open/09:00、open/09:30、after_close/15:30、every_bar；`handle_data` 每交易日开盘执行一次，传入的完整 bar 截止上一交易日。开盘只允许提交原生订单并按当日开盘成交，`get_current_data().day_open` 在开盘后可见；当日完整 OHLC/量额到 15:00 才进入历史。`get_price` 支持 count 或 start/end 日期，`attribute_history` 可跳过有明确停牌标记的源行，未观测缺口不填充。收盘记录原生权益，允许期末持仓；当日总成交量 25% 是明确的回溯容量模型。`on_strategy_end` 可读取结果并记录日志，不再下单。

日频与分钟共享数据准备、来源冻结、Python 回调、订单接口、日志、队列和结果发布；日频适配复用既有日线 Source / Matcher、原生账户和公司行动组件。CMES 只在 A数达解析，不在策略引擎复制。日频 `attribute_history/history/get_price` 的 `fq=None/pre/post` 通过同一 owner 价格接口投影，`get_bars(fq_ref_date=日期)` 支持显式前复权基准。默认基准为本次查询最后一个已可见交易日，避免用回测结束日因子处理较早决策；显式基准不得在当前时钟之后。复权只影响信号查询的 OHLC，量额、源 preclose、撮合与账户仍为原价。未配置固定因子版本时请求复权会明确报错。公司行动、上市日期、执行日停牌/ST、执行日 preclose 或日历缺失均阻断准备，复权因子不替代公司行动到账证据，因此本功能不代表纯 CMES 完整切换。

日频预热的 OHLC/量额完整性仍校验，但不要求只用于撮合价限的 `is_st` / `preclose`。预热的 `tradestatus` 在查询 `skip_paused=True` 时按实际读取范围校验；缺失返回 `PYTHON_DAILY_HISTORY_STATE_GAP`，附证券、日期、字段和策略行号。使用 `skip_paused=False` 可读取原始历史行，不推断其停牌状态。执行日期的交易状态、ST 和前收盘要求保持不变。

联调实例可在启动前显式设置 `ASHARE_QUANT_PYTHON_HISTORY_FACTORS=/path/to/binding.json`，文件为 `{"root":"/path/to/factor-store","dataset_id":"完整 SHA256"}`；相对 root 以绑定文件目录为准，不允许 latest。服务载入配置后将其纳入 environment_id；任务接受时复制并验证因子快照到独立任务目录，输入与准备报告记录版本、截止日、源摘要和限制，执行时不再读取可变选择器。此配置不改正式行情、默认数据或公司行动来源。候选 AD 依赖组合导入 81313c1 与因子 310e468，固定集成提交 c90839fa588103de53c1e3bb6da135c5d5df10b8。

HTTP 请求新增的频率和预热字段均进入冻结身份。旧分钟请求默认 `1m`，`python-minute-job.v1` 历史标签继续保留，通过 `execution_scope.frequency` 区分；旧任务仍需原固定源码版本读取。浏览器保留旧草稿并补默认频率，失败或取消后不会自动重跑。

分钟任务最多 8 股、23 个交易日、20,000 根股票分钟线且不跨越 31 个自然日；输入文件上限 64 MiB、总任务产物门禁 512 MiB，沿用共享队列 CPU、内存、输出和执行时限。边界按本轮真实样本的序列化大小设定，不代表底层引擎容量上限。

分钟数据采用 `source-labelled-corporate-date-model.v1`：按原标签构造已收盘模型，日状态于 09:00 回放，原价、每分钟量限制、下一分钟开盘撮合、T+1；涨跌停使用日线前收 5%/10% 研究模型。公司行动是冻结来源观测和日期到账模型，不宣称完整历史 PIT。初始与结束持仓均须为空，策略须自行在结束前平仓；不会自动平仓掩盖策略逻辑。确认价格无效的分钟经 AD 标准读取转为明确缺失，准备器沿用 `ad-minute-research.v1/unavailable_minutes` 保留时刻、原因和原件定位；历史接口在原时间位置返回 NaN，不成交、不产生新估值。单独 conflict 保留原值和 deny。未声明的源缺口、部分量额缺失、未知状态或不支持的公司行动仍返回具体错误，不补行情或转成价格诊断。09:30 等模型时段外标签在 preparation 报告列明，原件不改。

HTTP `/api/v1/python/catalogue` 返回示例和提交环境身份；`POST /jobs` 接受源码、参数、固定选择、环境身份、幂等键和两项明确确认。任务与日频/组合共享原有队列、租约、资源监控、取消和进程组终止机制。一个受控进程准备输入后调用共享 Python `run_native`，按频率选择既有日线或分钟适配器；没有第二套撮合引擎或嵌套失控进程。只对可信本地代码开放，这些限制不等于恶意代码沙箱。

浏览器先保存原请求再提交。重复点击/网络重试用同一键；环境变化时拒绝将未知结果自动转投另一个环境。返回或刷新只读取任务。取消停止进程、不发布结果；任意 Python 内存不支持断点恢复，中断任务只能由用户明确新建一次。旧源码版本的控制库继续使用原固定运行时，不原位绕过数据库兼容门禁。

任务目录保留源码、提交配置、发布身份、输入、准备报告、原生事件与结果，来源和运行依赖均冻结。结果显示区间收益、期末权益、日末最大回撤、成交、委托原因及日志；不从两日样本推算年化、夏普或盈利承诺。既有模拟账户与行情原件不写入。
