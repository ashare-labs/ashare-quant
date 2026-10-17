# 日频任务与策略

`tasks.daily` 提供有限日频服务；`backtests.daily` 表示决策和账本契约，`strategies.daily`/`strategies.python` 管理策略，`providers.daily` 消费 A数达公开数据。共同队列、锁、发布与进程控制在 tasks 和 storage。

HTTP 前缀 `/api/v1/tasks/daily`。`GET /jobs/catalogue` 返回窗口、参数、假设、绑定和源码身份；`POST /data/bind` 绑定配置 Store 内既有 dataset。`POST /jobs` 创建任务，`GET /jobs/{id}` 查询，后缀 `/manifest`、`/logs`、`/result` 读取冻结材料，`POST /cancel`、`/resume` 操作现有任务。完整字段见 [生成契约](contracts/tasks.openapi.json)。

CLI：`task-catalogue`、`tasks`、`task-data-bind`、`task-data-import`、`task-create`、`task-status`、`task-manifest`、`task-logs`、`task-cancel`、`task-resume`、`task-result`、`task-service`、`task-worker`。`--db` 指向新控制库，owner Store 由 `ASHARE_QUANT_DAILY_STORE` 配置。

`task-strategy-register --request RULE.json` 注册规则。`task-strategy-register-python --file SOURCE.py --descriptor DESCRIPTOR.json` 注册本地可信单文件 Python；HTTP 只能读取、选择 Python 版本。示例在 examples/rules 和 examples/python。版本冻结源码、参数和 SDK 摘要；文件修改须注册新版本。

`two_day`、`w1`、`w2` 是当前 AD 提供的数据窗口 ID；旧 `m2a` 已改为 `two_day`。可在准入交易日内选结束日期，不能借参数扩大数据域、证券或角色。旧准备态和封存结果 API 已退出；新任务只读取自己的执行回执。

取消保留检查点；恢复须核对源码、运行依赖、绑定和已发布材料。新版任务目录为 `.tasks`，不要直接接入旧库。共享 worker 处理日频和组合队列，各功能域保留自己的账本投影。
