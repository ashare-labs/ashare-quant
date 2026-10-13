# 任务服务可靠性与运维

本服务仍执行 `m2-jobs.v6` 固定研究任务。HTTP 请求、任务视图、策略版本、manifest 和结果的 v6 字段不变。新只读 `GET /api/v1/m2/jobs/service` / CLI `job-service` 使用独立 `m2-job-service.v1`，暴露服务策略摘要、各状态计数、过期租约数、未入库准备目录数和剩余待处理容量。相对 AQ `ad34f32` 的 v6 接口，本批次不添加必填请求字段。旧 quant-web `a1d211b` 仍固定 `m2-jobs.v1` / AQ `0.4.0.dev14`，不能直接使用本服务；须由前端更新契约和固定服务身份后接入。`data_binding_sha256` 在当前 v6 创建请求中必填，缺失返回 422。不能通过降低服务版本、移除输入摘要或关闭身份校验兼容旧客户端。完整和 M2 scoped OpenAPI 均包含新只读接口。

## 排队与并发

默认每个数据库一个执行槽、最多 32 个非终态任务，每个任务最多 8 次尝试。`queued/running/cancelling` 均占容量；取消排队任务即时变为 `cancelled`，运行任务先变为 `cancelling`，必须停止子进程并核实 checkpoint 后才能结束。重复取消没有额外副作用；取消与发布在同一 SQLite 写事务中决胜，取消不能产生成功结果。

新数据库可通过 `ASHARE_QUANT_JOB_POLICY` 设置更小的资源预算或最多四个槽。例如：

```sh
export ASHARE_QUANT_JOB_POLICY='{"max_workers":2,"max_pending":16}'
.venv/bin/ashare-quant --db .state/new-research.sqlite3 job-service
```

策略在首次打开数据库时写入独立的 `m2_job_service` 表，之后 API 和 worker 必须沿用同一策略。省略环境变量表示读取已经保存的策略；显式不同策略返回 `M2_JOB_POLICY_CHANGED`，不会静默增加并发。改变预算请选择新库。同一库可启动至多相应数量的 `job-worker` 进程，每个进程逐项工作；多启动的进程不会突破全库文件锁槽位。一个 worker 的非 `--once` 模式持续排队；Ctrl+C 会结束当前子进程并保留可检查的任务状态。

队列按 `created_at` 再按 SQLite `rowid` 排序，显式 `--job-id` 可指定任务。幂等键相同且输入相同，复用原账户，即使队列已满也如此；相同键修改输入返回 409。创建和恢复共享 admission 锁，等待默认最多两秒，竞争返回 `503 M2_JOB_ADMISSION_BUSY`；满队列返回 `429 M2_JOB_QUEUE_FULL`。HTTP 都带 `Retry-After: 2`，客户端应保留原幂等键重试，不能每次换键。

执行槽与每个账户的写锁由原生子进程继承，worker 被杀后也不会仅凭租约到期就再开一个账户写进程。准备与 checkpoint 检查另有一个全库锁，不占执行槽：默认最多一个执行进程加一个准备/检查进程；配置双槽时上限为两个执行加一个准备/检查。BLAS/OMP/MKL/NumExpr 均限制为一个线程。

## 进程与资源

每个子进程使用独立进程组。停止操作只针对本次实际创建的 Popen 进程组，绝不根据历史日志中的 PID 杀进程。取消或异常先发送 TERM，再在有界等待后发送 KILL；子孙进程同时结束。子进程自己的 watchdog 检查父进程存活、实际运行时长和 RSS，父 worker 死亡后自行退出；原有数据库 lease watchdog 继续检查 token、租约和取消请求。

默认预算：执行 120 秒、准备/检查 60 秒、单次 stdout/stderr 合计 1 MiB、每文件 64 MiB、进程峰值 RSS 1 GiB、CPU 120 秒、打开文件 256 个。准备/检查输出也经过同一有界 drain。RSS 每 100ms 抽样，允许抽样间瞬时超额；CPU、文件大小和文件描述符采用子进程内 `setrlimit`，不会改变 API 进程限制。资源 watchdog 会留下 `resource-limit.json`，注明墙钟、RSS、CPU 或文件上限原因；数据库失败历史也保留这一错误。不能把这些界限理解为恶意代码沙箱、全机资源配额或任意外部进程的约束。本机可信策略边界不变。

待处理量和每任务尝试数有界；已完成档案由操作者保留，服务不会自动删除旧结果或无限重试失败任务。API 和 CLI 的容量错误（ENOSPC、EDQUOT、SQLITE_FULL）返回 `STORAGE_FULL`，只读/权限错误返回 `STORAGE_PERMISSION_DENIED`，其它文件 I/O 错误返回 `STORAGE_UNAVAILABLE`。HTTP 为 JSON 503（`Retry-After: 2` / `Cache-Control: no-store`），CLI 为 JSON、退出码 2，均提供原幂等键重试说明；SQLite 锁竞争为 `DATABASE_BUSY`，其它 SQLite 拒绝为 409 `DATABASE_OPERATION_REFUSED`。错误响应不能证明先前提交一定失败：先修复空间/权限，再重发原始输入和原幂等键，已接受任务会复用，不能换键重建账户。空间不足或损坏应先保留证据，修复后按下面恢复流程处理。准备阶段中断但尚未入库的目录保留 `submission.json` 和已写证据；其数量也以 `max_pending` 为上限，达到上限拒绝继续创建并报告 `M2_JOB_PREPARATION_RESIDUE_LIMIT`。操作者确认目录未入库、且没有服务/worker 使用它之后，才可自行归档，服务不会猜测并删除。

## 崩溃与恢复

1. 查看 `job-status ID`、`job-logs ID` 和 `job-service`。worker 死亡后租约最多约 15 秒过期。运行中的旧子进程还持有锁时，返回 `M2_JOB_WRITER_BUSY`，不能强制忽略。
2. 对正常过期且没有显式错误的任务，重新启动相同固定版本的 worker 自动接续。已提交的原生 checkpoint 是唯一账户状态；完成后、发布前死亡也从同一 checkpoint 完成封存，不重做交易。
3. `failed/cancelled` 任务须显式 `job-resume ID`，检查冻结策略、Python 源码、参数、日期、数据绑定、session、checkpoint 摘要后重新排队，账户 ID 不变。尝试预算用尽返回 `M2_JOB_ATTEMPT_LIMIT`；不要直接编辑数据库清零计数，应在查明问题后创建新的任务。
4. `recovery_required=true` 只是租约过期，不证明旧进程已经退出。服务仍核实账户写锁。损坏文件、source 漂移或 checkpoint 摘要变化均拒绝恢复。

取消的任务不能读取成功结果；只有数据库已提交 `succeeded` 且发布目录全部文件摘要复核通过，`result` 才可读取。queued 日志记录完整固定服务策略及其摘要；成功发布把该历史和原生 receipt、策略、数据绑定、输入、执行身份一起封存。读取结果会重新验证封印，HTTP ETag 对应结果摘要。

## 数据库兼容与升级

启动只打开临时副本检查数据库及 WAL/journal，执行全库 `integrity_check`、外键检查，以及保留表、约束和索引 DDL 的结构比较。视图占用表名、缺索引、约束或列变化、控制版本 1/2、零字节现有文件均拒绝，不在原库执行启动 DDL、切换 journal mode 或自动 ALTER。原有完整 v3 控制表布局仍受支持；缺少服务表的旧库必须走显式新目录升级。

新库路径必须尚不存在；在同目录私有临时文件完成所有表与索引、WAL 配置、验证和 fsync 后，用 create-only 硬链接发布。检查或 DDL 失败不会留下半成品原库，竞争者创建的目标不会被覆盖或继续初始化。发布后目录 fsync 若失败，可能留下完整的新库，须先检查再沿用同一路径，不宣称发布回滚。已有库和固定策略的正常重启也不打开原件 SQLite 连接。首次固定策略的写入是校验通过后的独立事务。

本版 Store 的所有连接与启动检查共享每数据库文件锁：检查/首次策略入库持独占锁，业务连接持共享锁，SQLite 继续负责业务写事务互斥。锁保存在当前用户系统临时目录，服务不删除锁文件；运行期间不能清理这些锁或使用不同 TMPDIR 启动同库实例。文件锁不约束旧版服务或外部 SQLite 工具，这些工具须在启动检查/升级前停止，运行期间不要手工改库或经硬链接别名启动第二实例。副本复制和校验前后检查数据库/WAL/journal 文件签名，发现变化重试后拒绝；无法承诺阻止不参与锁的外部程序在任意时刻改写文件。已打开的 Store 还核实数据库设备/文件身份，删除或替换后返回 `DATABASE_FILE_CHANGED`，使用 `mode=rw` 连接，绝不静默重建。

旧 v3/v4/v5 清单返回 HTTP 409 / CLI 2 `M2_DATABASE_VERSION_INCOMPATIBLE`；空的旧式任务表、未知控制 schema、损坏 SQLite 也给出结构化拒绝，不在原库建表。同为 v6 但源码身份不同返回 `M2_DATABASE_EXECUTOR_INCOMPATIBLE`。SQLite `user_version=3` 是控制表版本，不等于任务清单 v3。

不可把旧 manifest 版本号直接改成 v6，或把旧任务的源码摘要替换成新摘要。不同策略/输入/执行身份必须创建新任务。升级命令把保留历史与新任务运行环境放在同一新目录，不承诺把旧 native checkpoint 转换为新执行核心：

```sh
# 先停止使用旧库的服务、worker 和原生子进程。
.venv/bin/ashare-quant --db /path/to/old.sqlite3 database-upgrade \
  --destination /path/to/new-generation
```

目标必须不存在。命令逐字节复制数据库、存在的 WAL/journal 及同名 `.m2-jobs` 目录到 `archive/`，拒绝符号链接，验证复制前后原件内容、大小和修改时间未变化。只打开临时副本进行 SQLite integrity/backup，输出合并 WAL 后的 `historical.sqlite3` 只读历史副本，以及空的当前 `control.sqlite3` 与 `upgrade.json` 文件清单。旧任务保留原 ID、状态和内容，不迁入新队列、不获得新版本执行授权。SHM 是瞬时索引，不用作历史恢复输入，命令也不打开或修改原件 SHM。

使用新库时重新绑定获准的 owner 数据并注册策略。读取旧任务继续用原固定后端与 `archive/原文件名` 及其同名任务目录；不要同时执行原件和归档副本。新版本从旧库返回的拒绝不是数据丢失，回退方法是停止新服务并在原固定环境重新打开未修改的原库。命令拒绝目标覆盖或链接，也不对原库做原地转换。成功清单 `upgrade.json` 最后发布；中断留下的目标没有该清单时不得当作完成，保留目标另选新路径重试。

浏览器修改请求仅接受 loopback 来源（localhost、127.0.0.1、::1）或无跨站标记的本机 CLI。非本机 Origin、null Origin 和无 Origin 的 cross-site 写入返回 403；不新增凭据。quant-web 本机代理来源仍可用。账户和结果没有写 API，额外请求字段仍由严格模型拒绝。它是本机可信用户边界，不是多租户鉴权服务。

## 可重复验证

工程用例仅使用 pytest 临时库。原生整链需要调用者提供有权使用的 M2 components；没有输入会明确跳过，不把合成数据冒充真实行情。

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
ASHARE_QUANT_TEST_M2_COMPONENTS=/path/to/components \
.venv/bin/python -m pytest -q tests/test_job_service.py tests/test_database_compat.py tests/test_database_admission.py \
  tests/portable/test_service_reliability.py tests/portable/test_recovery_process.py \
  tests/portable/test_native.py tests/portable/test_python_native.py \
  tests/portable/test_service_http.py tests/portable/test_storage_recovery.py
```

负载用例固定为八次提交、五个独立账户、最多三个并行提交线程和两个执行槽；检查满队列、幂等冲突、取消、并发峰值、结果一致和封存身份，输出 `load-evidence.json`。资源用例单次最多申请 96 MiB，并有 12 秒外部截止。故障用例覆盖 worker SIGKILL、被暂停的孤儿子进程、发布前死亡、四个原生 checkpoint 提交边界、KeyboardInterrupt 和 Python 失败，恢复后验证账户与成交不重复。另有真实 loopback HTTP + 两个独立 CLI worker 的 API SIGKILL/重启用例，以及结果提交事务失败、取消写事务拒绝和发布后取消竞争；失败事务不制造成功状态。HTTP 测试需允许本机监听。

补充回归涵盖非 manifest B-tree 损坏、保留名视图、索引/列/版本不支持、失败建表和 fsync、新库路径竞争、校验后 schema 变化、初始化与写事务相互等待、双进程不同服务策略竞争、删除/替换库、只读目录与容量拒绝；均仅在隔离临时副本进行。四类存储故障修复后按原键提交并实际执行，确认保留准备证据、只接受一个账户、成交 ID 不重复。
