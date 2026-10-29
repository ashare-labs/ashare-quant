# aquant 策略入口迁移

> 导入改写工具。策略运行与提交以 [Python 策略工作台](python-workbench.md) 为准。本页说明已保存文件从 `jqdata` 改为 `aquant` 的准备、审核和应用。部署配对见 [文档索引](README.md)。

策略运行 API 现名为 `aquant`。日频和分钟使用同一入口：

```python
from aquant import g, run_daily, record

def initialize(context):
    run_daily(observe, time="open")

def observe(context):
    record(equity=context.portfolio.total_value)
```

也支持 `import aquant as api` 和 `from aquant import *`。支持的函数、调度、取数、订单和账户语义不因更名改变。公开导出仍由运行时 `__all__` 给出；未支持的函数明确返回 `AQUANT_API_UNSUPPORTED`。本地 API 诊断前缀由 `JQ_` 改为 `AQUANT_`；历史日志与错误码不回写。旧导入的新提交返回 `AQUANT_LEGACY_IMPORT`，不会隐式改写提交源码或排入队列，也没有长期保留的 `jqdata` 模块别名。

## 安装与名称边界

`jqdata` 之前是 A宽在策略 worker 中注入的模块，并非本项目独立发布到 PyPI 的包；`aquant` 继续采用同样的运行时绑定。普通 Python 解释器在 worker 外没有策略账户/时钟，不能直接 `python strategy.py`。安装、命令行及源码包保持 `ashare-quant` / `ashare_quant`；A宽、A数达和仓库名保留。按 [依赖指南](dependencies.md) 构建固定 A数达 wheel，再用 `uv sync --locked --extra native`；离线已有缓存可加 `--offline`。也可安装本地构建的 `ashare_quant-*.whl` 和其锁定依赖，通过正常 Python 工作台或 CLI 提交运行。

2026-10-24 查验 [PyPI aquant 元数据](https://pypi.org/pypi/aquant/json)：名称已属于其他项目（0.0.0a0）。不要运行 `pip install aquant` 来安装本系统；无需发布 PyPI、申请名称或添加新凭据。wheel 仍只封装已有 `ashare_quant` 包，包含 aquant 运行时和迁移命令；没有虚构 `jqdata` 发行配置要重命名。

## 已保存策略：先准备，再切换

`ashare-quant-migrate-imports` 随 wheel 安装，也可用 `python -m ashare_quant.strategies.migrate_imports`。`--state-root` 必须是当前控制库所在目录，工具只递归访问其中的 `strategies/`，不会扫描任务、历史 manifest、数据库源码字段或行情库。

```sh
ashare-quant-migrate-imports prepare --state-root /path/to/state --output /path/to/review-bundle
ashare-quant-migrate-imports verify --bundle /path/to/review-bundle
```

准备只读现存文件，将原始 UTF-8 字节、候选文件、逐文件 diff、前后 SHA-256 和 AST 导入变更证明放进新建 bundle。保留 BOM、CRLF、注释、字符串及所有业务语句。`from jqdata import ...` 改模块名；显式别名保留。`import jqdata` 变为 `import aquant as jqdata`，这里 `jqdata` 只是策略局部变量，以保持既有引用语义，不是第二个运行入口。动态导入、子模块、语法不完整或无法确认的引用标为 `review_required`，整批禁止自动应用。`jqdatasdk`、注释和聚宽资料不机械替换。

集成者先审核 diff 与原文，完成独立回测。切换时等待已接受的旧任务终结，停止该部署 API/worker 及编辑器写入，备份并保留对应旧运行环境，然后运行：

```sh
ashare-quant-migrate-imports apply --bundle /path/to/review-bundle --control-db /path/to/state/control.sqlite3 --maintenance
ashare-quant-migrate-imports verify --bundle /path/to/review-bundle
# 需要回滚时：停写、恢复旧运行环境，并恢复原始导入。
ashare-quant-migrate-imports rollback --bundle /path/to/review-bundle --control-db /path/to/state/control.sqlite3 --maintenance
```

应用和回滚取得部署排他锁，并拒绝存在 queued/running/cancelling 等未完成任务的控制库。`--maintenance` 是操作人确认 API、后台工作器和编辑器已停写，不能替代停写操作；非标准启动器也必须由部署负责人停写。所有文件、备份和候选摘要先整体核验，写入采用已有安全原子文件存储。新增文件、并发编辑、链接替换或篡改候选均拒绝；中断后先 verify，重复 apply/rollback 可从已经确认的原文/候选状态继续。不要对冻结任务目录使用迁移工具，也不要替换历史源码以使校验通过。旧运行环境身份已冻结的任务不能在新包下恢复执行；历史只读展示和结果校验保持原证据。

## 浏览器草稿与跨仓调用

浏览器草稿按设备和 Origin 分离；服务端文件迁移不会改写 localStorage。导出各浏览器草稿到独立临时 `state/strategies/` 目录，用 prepare 审核候选，再导入编辑器，检查日期、资金和频率后显式保存。保留导出原件；不要从历史 manifest 覆盖当前草稿。历史源码页面继续只读展示 `jqdata` 原文。

quant-service 传递源码到同一 AQ 后端，其当前 acceptance/follow/joint 脚本均使用 aquant。若另有 quant-service 产品库的 `strategies.definition.source`，先通过现有资源接口导出原定义及版本，将 source 导出到上述临时文件目录审核；待跟随任务终结后，使用现有带版本校验的更新接口保存新定义。任务 payload、follow_commands 和已固定的 source/version 不回写。本迁移命令不会自动修改任意 SQLite JSON 文档。

## 保留的旧词与验证边界

- `tests/fixtures/minute_reference/gap_probe.py` 是已在第三方聚宽执行、带固定摘要的原始观察源码，保留 `jqdata`。本地 `gap_ported.py` 使用 aquant。
- 双平台观察脚本生成器对 `platform="jq"` 生成第三方 `jqdata`，对 `platform="aq"` 生成 aquant，不能把两边改成同名 API。
- `jq_current`、`jq_minute`、`jq_daily_initial` 等已有撮合/生命周期协议值描述聚宽参考模型，仍参与历史输入摘要，保持原值。
- 迁移识别器、旧入口拒绝测试及本指南必须保留旧拼写以识别和说明迁移；这不构成可执行旧入口。
- A数达有关 `jqdatasdk` 的第三方依赖、来源及差异说明保持准确。

合成回归验证工程路径；真实策略/真实发布联验须单独记录源码和数据摘要、频率、区间、成交及账户比较。未运行的外部聚宽对照不得据此宣布等价。本次更名不触发上线、主分支合并或正式策略修改，部署由集成负责人统一切换。
