# 研究任务

自定义 Python 日频和分钟共用 `/api/v1/python`、同一任务表、租约、资源预算和 worker。因子通过 `/api/v1/factors` 使用相同队列。`GET /api/v1/tasks/service` 返回共享资源状态，`GET /api/v1/tasks/openapi.json` 返回当前任务契约。

`POST /api/v1/python/jobs` 接受用户源码、参数、频率、证券、日期、资金、环境身份、幂等键和两项明确确认。GET 列表、详情、manifest、logs、result 均不执行策略；POST cancel 请求取消。

网络中断时用原幂等键找回任务，应用重启后读取原冻结结果。任意 Python 内存没有可序列化检查点：`can_resume=false`，POST resume 明确返回 `PYTHON_RESTART_REQUIRED`。不会把重试提交、恢复结果读取、重跑策略混成一个动作。需要重跑时由用户明确创建新任务与新键。

worker 只处理当前源码的 Python 和因子任务；退役固定窗口、七算法、旧源码的待处理记录保持原样。来源、数据和运行时身份仍在执行前校验，读取旧结果不要求安装旧策略算法。旧待执行记录通过 display_status 明确为 retired / runtime_unavailable，附 execution_reason，原始 stored_status 保留。当前 counts/available_pending 只统计当前可执行队列，历史数量在 historical_counts 单列，不能让它们阻塞新研究。

存储预算分为三项：`artifact_bytes` 是**单个任务**保留产物的上限，默认 512 MiB（536,870,912 字节）；`retained_artifact_bytes` 是所有任务产物的总留存上限，默认 **512 GB（512,000,000,000 字节，十进制，不是 512 GiB）**；`disk_free_bytes` 是同一文件系统的最小空闲储备，默认 1 GiB（1,073,741,824 字节）。新建任务时检查一次历史文件元数据，并为当前待执行任务的剩余额度及新任务预留空间；历史不占用新任务的单任务额度。幂等重试仍直接返回已有任务。旧数据库的原 policy 文档及摘要不改写，读取时为新增总留存和储备字段补默认值；状态响应明确 `artifact_budget_scope=per_task`。

Python 与因子共用这些预算。worker 在启动前、执行期间（约每 250 ms）、退出后与发布时只检查本任务产物及磁盘空闲量，不重复扫描历史；发布前计入即将写入的冻结结果。单任务超限返回 `M2_JOB_ARTIFACT_LIMIT`（含实际字节、限额和 per_task 作用范围）；总留存及排队预留超限返回 `M2_JOB_RETENTION_LIMIT`；真实磁盘余量不足返回 `M2_JOB_STORAGE_RESERVE`（含可用量、储备及排队预留量）。运行中触发会停止自己的子进程组并记录失败，不发布成功结果、不删除证据。采样检查允许检查间隔内的短暂增长，单文件、输出、CPU、内存和时间限制仍独立生效。历史行情快照与事件日志保留，不为解除准入错误自动清理历史。

`GET /api/v1/history/jobs` 可按 family=daily/portfolio/python/factors 筛选；详情、manifest、logs 和 result 为只读冻结记录。结果核对数据库身份、接受清单、会话及发布摘要；老封存格式还核对 seal 中每份文件。响应中的原源码身份不会改成当前版本，此校验不重新审计历史策略收益。

CLI 提供 `python-create --request`、`task-worker`、`task-service`、`history`、`history-strategies`、`task-status/manifest/logs/result/cancel/resume`。已删除 `task-create`、固定数据导入绑定和预设策略注册命令。详见 [迁移清单](formal-backend-migration.md) 和 [生成契约](contracts/tasks.openapi.json)。

## 配置更新和历史读取

服务策略是当前实例的新任务默认值，不再属于数据库身份。设置 `ASHARE_QUANT_JOB_POLICY` 后重启同一实例即可使用原控制库；原 `task_service` 文档和哈希不改写。每个新Python/因子任务在已验收会话内冻结 `resource_policy`；旧Python任务使用原 `data_budget`，更早未带任务策略的记录使用原库首次策略。已排队或运行的任务继续使用其冻结的执行/数据/产物/重试预算，新的提交使用新默认值；准入并发、总留存和实际磁盘空闲保护取当前实例设置。状态中的 `policy_sha256` 为当前默认摘要，`initial_policy_sha256` 保留原库摘要，`execution_policy_scope=frozen_per_task`。代码、数据发布、任务哈希和数据库schema检查继续生效。

未接入原生引擎的 `module_load_seconds` 和 `callback_seconds` 已从现行配置、生成契约和worker删除，不新增回调超时门槛。旧证据中保留这些字段时可读兼容，既有环境JSON中的这两个废弃键兼容忽略并发出明确警告，状态与生成契约不再展示，不能因此拒绝启动；原有全任务墙钟、CPU、RSS、文件和输出监督继续生效。

合法发布结果不再受另一个64MiB读取门槛限制，也不依赖当前实例较小的写入预算。HTTP结果在校验完整原始SHA后以64KiB块流式返回，JSON结构不变；临时私有副本在请求结束或断开时关闭，不改原结果。历史输入哈希按块计算。分析/列表只解码账户、摘要、准备信息等必要字段，跳过无关日志；派生指标继续由同一计算器给出。真实临时磁盘不足或内存分配失败单独报告，不伪造成功或删除历史。
