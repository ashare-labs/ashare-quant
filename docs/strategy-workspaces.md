# 策略存储工作空间

供同机可信产品服务使用的存储命名空间，不包含账号业务，不是 Python 沙箱。AQ HTTP 和 stdio MCP 仍属于可信本机所有者；不要把这个标识当成用户凭据，也不要向朋友直接暴露引擎入口。

`GET /api/v1/python/workspace-capabilities` 返回 `aq.strategy-workspaces.v1`。产品服务必须先检查该契约，旧引擎可能忽略未知 Header，不能降级到全局目录。

文件、目录、完整项目接口与 `POST /api/v1/python/jobs` 接受 `X-Quant-Workspace`：32 位小写十六进制不透明标识，由产品服务从已鉴权 owner 的持久映射选择。标识不是文件路径。无 Header 保持既有 `state/strategies`；有 Header 使用 `state/workspaces/<handle>/strategies`。新目录只有写入时才创建，GET 不创建目录。原有目录遍历、符号链接／硬链接防护、原子保存、项目版本及大小写规范身份逻辑保持使用。

回测准入在相同空间检查已保存文件／项目，冻结 `session.strategy_workspace`，完整源码仍取本次提交快照。重试相同核心幂等键但更换空间会拒绝；原有无命名空间任务及其冻结内容不改写。账号后端仍须对核心幂等键、任务、历史、结果及取消操作做 owner 授权；AQ 的原始任务列表不是多用户接口。

不更改既有执行预算或启动策略。首期账号后端禁止朋友提交／重试／推进 Python，直至独立执行隔离验收。本改动没有提供对任意 Python 读取主机文件的隔离保证。

验证使用临时合成文件和任务：不同空间的同名文件／项目、猜测项目 ID、路径与链接越界、源码保存不执行、空间随任务冻结及跨空间幂等冲突。只对源码和隔离测试环境实施，生产目录、监听配置与现有任务不迁移。

相关现有契约：[单文件策略](strategy-files.md)、[完整项目](strategy-projects.md)。
