# 研究任务

自定义 Python 日频和分钟共用 `/api/v1/python`、同一任务表、租约、资源预算和 worker。因子通过 `/api/v1/factors` 使用相同队列。`GET /api/v1/tasks/service` 返回共享资源状态，`GET /api/v1/tasks/openapi.json` 返回当前任务契约。

`POST /api/v1/python/jobs` 接受用户源码、参数、频率、证券、日期、资金、环境身份、幂等键和两项明确确认。GET 列表、详情、manifest、logs、result 均不执行策略；POST cancel 请求取消。

网络中断时用原幂等键找回任务，应用重启后读取原冻结结果。任意 Python 内存没有可序列化检查点：`can_resume=false`，POST resume 明确返回 `PYTHON_RESTART_REQUIRED`。不会把重试提交、恢复结果读取、重跑策略混成一个动作。需要重跑时由用户明确创建新任务与新键。

worker 只处理当前源码的 Python 和因子任务；退役固定窗口、七算法、旧源码的待处理记录保持原样。来源、数据和运行时身份仍在执行前校验，读取旧结果不要求安装旧策略算法。旧待执行记录通过 display_status 明确为 retired / runtime_unavailable，附 execution_reason，原始 stored_status 保留。当前 counts/available_pending 只统计当前可执行队列，历史数量在 historical_counts 单列，不能让它们阻塞新研究。

`GET /api/v1/history/jobs` 可按 family=daily/portfolio/python/factors 筛选；详情、manifest、logs 和 result 为只读冻结记录。结果核对数据库身份、接受清单、会话及发布摘要；老封存格式还核对 seal 中每份文件。响应中的原源码身份不会改成当前版本，此校验不重新审计历史策略收益。

CLI 提供 `python-create --request`、`task-worker`、`task-service`、`history`、`history-strategies`、`task-status/manifest/logs/result/cancel/resume`。已删除 `task-create`、固定数据导入绑定和预设策略注册命令。详见 [迁移清单](formal-backend-migration.md) 和 [生成契约](contracts/tasks.openapi.json)。
