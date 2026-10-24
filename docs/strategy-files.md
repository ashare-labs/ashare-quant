# 本机策略文件

已有导入迁移使用 [aquant 迁移工具](aquant-migration.md)，先生成可审核候选，停写后显式应用；文件读写接口不会自动改源码。

策略目录是正式部署 `product` profile 的 `control_db` 所在目录下固定的 `strategies/`。
例如 `control_db=/data/ashare/state/control.sqlite3` 时，策略根为
`/data/ashare/state/strategies`。沿用现有 deployment 发现、初始化和配置变更检查，
没有新配置文件、数据库表或策略根环境变量；客户端不能传入绝对根目录。
未配置正式部署时返回配置错误，不回退到工作目录或浏览器存储。
`GET /directories` 返回当前根路径，未创建时为空列表，读取不创建目录或示例文件。

本能力复用 `storage.artifacts.atomic` 的完整文件发布，复用 `/python/` 的本机来源检查、
错误响应与 `Cache-Control: no-store`。旧 `registered_strategies`、`/strategies` 与
`/history/strategies` 是历史冻结记录，不作为可编辑文件库。
现有部署 state backup/restore 将 `strategies/` 随 state 文件一起保留；不另建策略快照系统。

## HTTP 契约

以下路径统一以 `/api/v1/python` 为前缀。成功均返回 `200`，请求/响应为 JSON。
`path` 是根下相对路径，使用 `/` 分隔；目录名和文件名支持中文。
只列出直接子目录及普通 `.py` 文件，可按目录逐层读取。

| 方法与路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `GET /directories?path=研究` | `path` 可省略或为空，表示根 | `{"root":"/data/ashare/state/strategies","path":"研究","entries":[{"name":"日频","path":"研究/日频","kind":"directory"}]}` |
| `POST /directories` | `{"path":"研究/日频"}` | `{"path":"研究/日频"}`；递归创建必要父目录，已有目录成功 |
| `GET /files?path=研究/日频/动量.py` | 必填 `path` | `{"path":"研究/日频/动量.py","source":"用户源码"}` |
| `PUT /files` | `{"path":"研究/日频/动量.py","source":"用户源码","overwrite":false}` | `{"path":"研究/日频/动量.py"}` |

保存第一次使用时按需创建固定策略根，嵌套父目录须先用建目录接口创建。
`source` 按 UTF-8 原文写入，保留换行，不解析、导入或执行代码。
允许空文件和未完成的 Python；读写上限均为 256 KiB UTF-8 字节，与回测源码预算一致。
只有小写 `.py` 后缀可读写，读取非法 UTF-8 明确失败，不自动转码。

`overwrite` 默认 `false`，已有文件返回 `409 STRATEGY_FILE_EXISTS`。
显式 `true` 才替换现有文件，也可创建不存在的文件；并发覆盖以最后成功发布的完整内容为准，
不增加版本管理或编辑冲突系统。并发创建同一路径仅一方成功。
写入先完成同目录临时文件并刷盘，再原子发布并同步目录；发布前失败保留旧文件，尽力清理临时文件。
发布后同步失败会返回不确定状态，不能显示“保存成功”；应重新读取确认。

| 状态 | 错误码 | 含义 |
| --- | --- | --- |
| `422` | `STRATEGY_PATH_INVALID` | 非法路径或非 `.py` 文件；路径至多 1024 UTF-8 字节、每段至多 255 字节 |
| `422` | `STRATEGY_SOURCE_ENCODING` / `STRATEGY_SOURCE_SIZE` | 非法 UTF-8 / 超过源码预算 |
| `422` | `STRATEGY_QUERY_UNSUPPORTED` | 未知或重复查询参数；非法 JSON 字段/类型沿用框架校验响应 |
| `403` | `STRATEGY_PATH_UNSAFE` | 符号链接或多硬链接文件；不在目录列表中展示 |
| `404` | `STRATEGY_NOT_FOUND` | 文件或父目录不存在 |
| `409` | `STRATEGY_PATH_CONFLICT` / `STRATEGY_FILE_EXISTS` | 文件/目录类型冲突，或没有明确允许覆盖 |
| `503` | `STORAGE_FULL` / `STORAGE_PERMISSION_DENIED` / `STORAGE_UNAVAILABLE` | 磁盘、权限或其他存储错误；保存响应 `error.details.save_state=not_saved` |
| `503` | `STRATEGY_SAVE_UNCERTAIN` | 已发布但持久化未确认，`save_state=uncertain`；重新打开核对 |

沿用全局 `{"api_version":"v1","error":{"code":"…","message":"…","details":{…}}}`。
权限检查从已解析的部署 state 路径逐级以目录描述符打开，拒绝符号链接；
所有文件操作相对于检查过的目录，不将用户输入拼成任意本机绝对路径。
禁止绝对路径、`.`、`..`、空路径段、反斜线、冒号和控制字符。
新目录权限为 `0700`、文件为 `0600`。这里的边界防止 HTTP 路径逃逸，不改变本机用户对其文件的控制权。

## 前端未来接入现有按钮

本次只增加后端接口，没有修改 quant-web 或接入前端 API。
未来“新建文件夹”使用 `POST /directories`，目录列表用 `GET /directories`，
点击策略用 `GET /files`，编辑器“保存”用 `PUT /files`；新建策略可以保存空的 `.py`。
已有文件保存明确传 `overwrite=true`，新文件同名错误由现有反馈区域展示。
只有成功响应后更新“已保存”，失败保留编辑内容；网络中断或 `uncertain` 应重新读取核对。

当前 `quantweb-strategy-drafts-v1` 和 `quantweb-folders-v1` 不自动迁移、合并或删除。
文件只存 Python 源码，日期、资金等运行配置仍按前端未来方案处理，不隐式写入旁车文件。
运行时继续显式提交源码与配置至现有 `POST /api/v1/python/jobs`；该接口已有的
`strategy.py`、`submission.json`、manifest 和结果冻结保持原语义，保存文件不会修改历史回测快照。
没有新增重命名、删除、版本管理、自动运行或第二套任务平台。

文件 entry、读取与保存响应现同时返回 `strategy_id`，值为完整相对文件路径。
该值可随回测提交建立明确归属，语义和分页接口见 [策略所属回测](strategy-backtests.md)。
目录响应不增加策略身份；保存请求仍只使用原 path/source/overwrite 字段。

返回的 path/strategy_id 使用文件系统实际目录项拼写。大小写不敏感卷上的别名及系统等价
Unicode 名称解析到同一普通文件时返回同一身份；不统一转小写，不合并大小写敏感卷上的
不同文件。覆盖别名时保留实际文件名，目录响应的父路径也使用实际拼写。
访问期间目录项身份变化会返回 `409 STRATEGY_PATH_CHANGED`，重新读取即可。
客户端应使用读取/保存响应中的身份建立编辑、提交和列表导航，不能用 URL 原始别名覆盖它。
