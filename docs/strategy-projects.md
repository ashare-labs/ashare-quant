# 多文件策略项目

> 现行契约：多文件项目。工作台行为见 [Python 策略工作台](python-workbench.md)，地图见 [文档索引](README.md)。

一个项目是普通目录，默认入口 `main.py`，包含辅助 Python 文件和子目录。分类文件夹可嵌套，
项目不能再包含项目。旧 `.py` 策略、原有 API 和已冻结的单文件历史继续保留。

系统创建项目时生成 `aquant.toml`，只包含 `format_version = 1`、`project_id = "aqproj-…"`
和 `entrypoint = "main.py"`。项目身份与路径分离；改名、跨分类移动继续关联原回测。
日期、资金、频率及运行状态不写入该描述文件。新建独立项目必须获得新身份；直接复制描述文件
产生重复身份会明确拒绝读取/提交，不能把两个目录的结果混合。

## 编辑与保存

沿用策略列表、可选分类、项目、编辑器、独立结果页。辅助模块不作为独立策略显示。
编辑器左侧文件树支持文件切换、新建、改名、删除与入口选择。一次保存或回测提交使用完整
当前文件映射，包括未保存文件；保存不会提交回测，回测也不会自动保存磁盘。
项目内路径使用合法 Python 模块名与 `.py` 后缀，子目录按包处理；中文标识符可用。
拒绝路径逃逸、链接、大小写/Unicode 等价冲突和同名模块/包冲突。
删除或改名被其他文件导入的模块时，策略作者须同步修改 import。

文件集合限于项目 Python 源码和系统描述文件。第三方库、虚拟环境、数据集、缓存及二进制
不进入源码包；遇到未支持的磁盘文件会报告，保存不能静默删掉它们。
没有新增文件数量或期限上限。整项目源码使用已有 `artifact_bytes` 产物预算，不设单文件大小门槛，
`/python/catalogue` 可查询当前配置；冻结请求、manifest 和源码均计入实际任务产物预算。

HTTP 前缀 `/api/v1/python`：

| 方法 | 请求 | 用途 |
| --- | --- | --- |
| `GET /projects` | `?path=分类/项目` 或 `?strategy_id=aqproj-…`，二选一 | 完整读取；稳定身份可找到移动后的目录 |
| `POST /projects` | `{path, entrypoint?}` | 创建目录、描述文件和初始入口 |
| `PUT /projects` | `{path,strategy_id,entrypoint,files,expected_revision}` | 保存全部文件；缺失于映射的旧模块被删除 |
| `POST /projects/move` | `{path,destination,strategy_id,expected_revision}` | 项目整体改名或移动，目标不得已存在 |

响应为 `{path,strategy_id,entrypoint,files,revision}`。`revision` 是内容摘要，不依赖路径。
过期 revision 明确冲突，保留编辑器草稿供核对，不覆盖另一编辑者的版本。保存使用目录原子
交换（macOS `renameatx_np`，Linux `renameat2`）；不支持此能力时拒绝保存，不退化为逐文件
部分覆盖。交换后持久化确认失败返回 `STRATEGY_SAVE_UNCERTAIN`，保留原目录恢复副本，
应重新读取核对后处理。普通 `/files` 与 `/directories` 不能穿入项目边界。

## 导入与冻结

入口及辅助文件运行在同一次回测专属的 Python 包中。推荐写法：

```python
# main.py
from aquant import set_universe
from .signals import choose

def initialize(context):
    set_universe(choose())
```

```python
# signals.py
def choose():
    return ["000001.XSHE"]
```

`from .signals` 指向项目内模块，`from .models.signals` 指向子目录，子包内可使用
`from ..settings`。`__init__.py` 可选，也可作为入口。裸 `import signals` 仍遵循安装环境；
不把项目目录加入 `sys.path`，避免与标准库、第三方库或另一个项目同名模块发生冲突。
`import aquant` 始终使用本次回测绑定的运行接口；如项目自己有 `aquant.py`，仅通过
`from . import aquant` 访问。入口与辅助模块显式导入 API 时共享同一个运行上下文。

项目请求的 `project={project_id,entrypoint,files}` 与 `source` 互斥，`strategy_id` 必须等于
`project_id`。全部源码写入任务的 `source/`；`manifest.files` 固定逐文件 SHA256，session
固定完整项目摘要。未被 import 的文件也校验，历史页面显示/比较全部文件。修改任意辅助模块
都会改变请求与项目身份摘要。运行只读取冻结清单中的文件，不读取编辑目录或后来修改的模块。
任务开始、延迟导入及结束均校验冻结代码，历史读取复用原 `History.check`。
这里的隔离防止项目模块串用，可信本地 Python 的既有安全边界不变。

## 部署与回退

升级不扫描改写现有用户策略，不自动把分类目录转换成项目，不改写旧 manifest 或历史结果。
前后端须成对更新；无需新增 SQL 表或一次性数据迁移。部署前按现有流程暂停新写入并备份
完整 state（包含数据库、任务证据和 `strategies/`），保留当前运行包与 Web 构建供恢复。

旧后端不能读取新 `python-project-job.v3` 任务。若升级后已接受项目任务，回退旧运行包必须
配套恢复升级前 state；先完整保留升级后的 state 以便恢复新回测，不能删新行或重写历史格式
来强行兼容。尚无新项目任务时仍应由服务负责人核对状态再回退。工作树验证不切换正式服务。
