# 策略所属回测

旧单文件的完整相对路径是策略身份 `strategy_id`，包含全部目录；同名不同目录不会混合。
文件列表的 file entry、读取文件和保存成功响应均返回该身份。
同一路径覆盖保存保持身份；另存为、外部改名使用新身份。删除后重用路径仍代表同一策略槽位，
独立策略应使用新路径。本接口没有新增改名、删除或实体注册流程。

[策略项目](strategy-projects.md) 使用 `aquant.toml` 中系统生成的 `aqproj-…` 稳定身份。
项目改名或移动保留该身份，历史继续归属原项目；项目内辅助文件没有独立回测身份。
重复复制同一描述文件产生身份冲突，必须先解决冲突，不能按路径任意选取一个项目。

## 提交与旧记录

`POST /api/v1/python/jobs` 接受可选 `strategy_id`。提供时必须指向当前已保存普通 Python 文件，
不要求提交源码与文件内容相等：回测冻结用户明确提交的源码，未保存的编辑也属于该文件策略。
客户端将身份放入原有提交快照，网络重试沿用同一身份、源码、参数、环境和幂等键。
同一幂等键改变身份会返回冲突；已接受的同请求重试先返回原任务，不依赖后来文件是否还存在。

项目请求必须提交 `project={project_id,entrypoint,files}` 和相等的 `strategy_id`，与单文件
`source` 互斥。`files` 包含整个项目当前源码，包括所有尚未保存的文件；提交不自动保存磁盘。
项目身份必须存在且唯一，但不会用磁盘内容替换用户提交的草稿。

新任务按文件系统返回的规范相对路径冻结身份；提交别名与规范路径不产生两组新记录。
原始别名仅作为该次接受的 session 元数据保存，原请求及其规范响应都可幂等重试；
其他路径或内容变化仍返回冲突。旧任务的精确身份不改写，也不按当前文件别名猜配。

省略身份的旧请求保持原始序列化、摘要及 manifest，不回写旧任务。纯浏览器草稿未保存时可继续提交，
但记录属于“未归属”。不根据 basename、名称、源码 hash 或浏览器缓存猜测归属；同源码可能是多个策略。
原有历史详情、源码、日志与结果接口继续可读。

## 查询

`GET /api/v1/python/backtests?strategy_id=研究%2F动量.py&limit=25&offset=0`

包含此策略全部 Python 回测，跨当前及旧运行时，保留成功、失败、取消、排队和运行记录。
按 `created_at DESC, job_id DESC` 排序，`limit` 为 1～100、默认 25，`offset` 为非负整数。
省略 `strategy_id` 只列未归属记录，不将全部策略混为一组；文件已删除也仍可按原身份读取历史。
查询始终精确匹配冻结身份；当前文件入口先使用 `/files` 返回的规范 strategy_id。
旧别名记录仍可按旧身份查询，不自动合并到新记录。

响应字段：`strategy_id, identity_model="saved_relative_path", total, unassigned_count,
offset, limit, items`。`unassigned_count` 为全部 Python 未归属记录数量，便于如实说明历史缺口。
查询项目时 `identity_model="stable_project_id"`；旧文件身份及旧任务字节不迁移、不重写。

每行包含：

- `job_id, strategy_id, name, start_date, end_date, frequency, initial_cash`
- `created_at, updated_at, status, stored_status, display_status, execution_state, execution_reason`
- `can_cancel, error, result, result_available, result_error, summary`

`summary` 直接来自通过完整性验证的原冻结结果，不重算收益或风险指标。没有结果时为 null，
未计算指标保持缺失，不以 0 代替。`result.href` 指向原 `/api/v1/history/jobs/{id}/result`。
结果验证失败时保留该行、返回 `result_error` 和 `result_available=false`。
取消仍调用 `/api/v1/python/jobs/{id}/cancel`，仅 `can_cancel=true` 时显示操作。
冻结任务自身完整性失败仍明确报错，不以空列表掩盖。

客户端在策略列表、单次结果、历史只读源码和当前文件编辑之间保留同一 `strategy_id`。
旧 request 没有身份时显示未归属，不猜配。接口使用现有状态库及不可变任务证据，没有第二份运行记录。
