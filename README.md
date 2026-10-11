# A宽（ashare-quant）

## 项目简介

A宽是面向本机使用的 A 股量化研究后端，提供策略注册、持久任务、CLI、HTTP API、运行记录和结果查询。真实研究的数据由 A数达提供，交易账户由 RQAlpha 维护。仓库自带合成示例，便于先了解任务和结果的使用方式。

当前版本为 **0.11.0.dev14**。日频任务支持固定证券和窗口内的可信 Python 策略及 JSON 规则；分钟事件、跨日持仓、费用和公司行动另有固定研究入口。这些实验能力不代表服务支持任意股票、日期或完整聚宽 API。

现有 38 项评估为 **25 项有限范围主目标满足、4 项实质差异、6 项证据不足、2 项不适用、1 项工程验证**。范围见[能力与比较边界](docs/compatibility.md)。

## 安装指南

需要 Git、[uv](https://docs.astral.sh/uv/) 和 Python **3.12.13**。以下命令在项目根目录执行；运行环境固定到该 Python 补丁版本和仓库锁文件。

本版本依赖 A数达 **0.8.0.dev2** 的[固定公开提交](https://github.com/ashare-labs/ashare-data/commit/0c984cdeca6c63c684e91343955701c9b4e28977)。以下命令从公开 GitHub 获取这份源码，在本机构建并核对固定 wheel，不需要私人 checkout、账号或预置行情。首次安装需要访问 GitHub 和 PyPI；仅在具备相应使用权限时执行，现有[许可声明](docs/licensing.md)保持不变。

```sh
uv python install 3.12.13
git init .state/ashare-data-source
git -C .state/ashare-data-source fetch --depth=1 https://github.com/ashare-labs/ashare-data.git 0c984cdeca6c63c684e91343955701c9b4e28977
git -C .state/ashare-data-source checkout --detach 0c984cdeca6c63c684e91343955701c9b4e28977
uv run --no-project --python 3.12.13 python scripts/stage_owner.py --source .state/ashare-data-source
uv sync --locked --no-dev
```

如已有包含该提交的合法 checkout，可跳过三个 Git 步骤，将 `--source` 改为其路径。构建脚本只读固定提交，核对 wheel SHA256 后写入被 Git 忽略的 `local-dependencies/`。`--no-project` 用于先准备依赖，避免提前同步尚不存在的 wheel；显式 Python 版本避免调用系统旧版解释器。不要用最新 A数达版本替换固定提交。

默认安装可运行下方合成示例、查看 API、注册策略。需要真实研究执行时，先阅读[许可说明](docs/licensing.md)，再安装可选引擎：

```sh
uv sync --locked --no-dev --extra native
```

安装引擎不会取得行情或配置数据。后续使用 `.venv/bin/` 中的命令，可保留已选择的依赖组合。

## 快速开始

以下示例使用仓库自带的人工合成输入，不需要行情包、平台账号或浏览器会话。

```sh
.venv/bin/ashare-quant --db .state/demo.sqlite3 demo
```

预期 JSON 中 `state.run.status` 为 `succeeded`，`state.is_test_double` 为 `true`，`state.account.cash` 为 `9000.00`，`state.fills` 有一笔成交，`manifest.real_strategy_executed` 为 `false`。这演示任务、记录和查询流程，不是一次真实市场回测。相同数据库和默认幂等键重复运行会复用已有结果。

启动本机 API：

```sh
.venv/bin/ashare-quant --db .state/demo.sqlite3 serve --port 8807
```

打开 <http://127.0.0.1:8807/api/v1/docs> 查看接口文档。在另一个终端检查服务与运行列表：

```sh
curl --fail http://127.0.0.1:8807/api/v1/health
curl --fail http://127.0.0.1:8807/api/v1/runs
```

按 `Ctrl+C` 停止服务。数据库保存在 `.state/`，不应提交或随源码分发。未配置真实数据时，`job-catalogue` 返回 `configured=false`，是正常的未配置状态。

## 使用指南

**查看运行。** 从演示输出取得 `run_id`，用 `state RUN_ID`、`manifest RUN_ID`、`logs RUN_ID` 读取状态、身份和日志。命令前使用同一个 `--db` 路径；`ashare-quant --help` 列出全部入口。

**准备数据。** 经固定 A数达接口生成并验证有权使用的数据导出，再配置 Store、导入并查看目录：

```sh
export ASHARE_QUANT_M2_STORE="$PWD/.state/owner-store"
.venv/bin/ashare-quant --db .state/research.sqlite3 job-data-import --input /path/to/authorized-product
.venv/bin/ashare-quant --db .state/research.sqlite3 job-catalogue
```

窗口、日期、摘要和研究假设以自己的 catalogue 为准。缺项或 BLOCKED 时先处理数据条件；A宽不会自动取数、补行情或把未知事实当作零。任务流程见[研究任务](docs/research.md)。

**注册策略。** 仓库提供[买入持有](examples/python/hold.py)、[现金条件](examples/python/cash.py)、[分段仓位](examples/python/staged.py)及对应 [JSON 规则](examples/rules/hold.json)。注册不会执行源码：

```sh
.venv/bin/ashare-quant --db .state/research.sqlite3 job-strategy-register-python --file examples/python/hold.py --descriptor examples/python/descriptor.json
.venv/bin/ashare-quant --db .state/research.sqlite3 job-python-strategies
```

输入、返回值和资源限制见[策略编写](docs/strategies.md)。真实任务使用 `job-create`、`job-worker --once`、`job-result JOB_ID`，请求与恢复说明见[研究任务](docs/research.md)。

**多股整链题。** 独立本机入口支持固定八股的等权、动量和均线过滤题，信号、共享现金、原生订单与权益逐日留痕。准备真实 AD 输入后按[日频多股验收](docs/daily-portfolio.md)运行；合成运行不等于真实数据或 JQ 一致验收。

**诊断与开发。** `source-diagnostics-capabilities` 查看配置；只读检查需独立固定的 reader 环境及已有数据，见[数据诊断](docs/diagnostics.md)。测试和发行包构建见[开发与打包](docs/development.md)。

**连续账户基础验收。** 独立的 `python -m ashare_quant.paper` 入口可验证合成日意图、原生跨日账户、故障恢复与风险告警，见[日频连续模拟契约](docs/daily-paper.md)。真实数据、策略调度和人类前端联验仍待接入，能力接口明确返回未完成真实连续模拟验收。

## 限制与说明

- 日频任务服务仍限 `600000.XSHG`、`m2a/w1/w2` 固定窗口及连续前缀、`0/100/200` 股目标。实验入口不自动扩展服务范围。
- B25、B26、C02、C03 保留实质差异；B12、B14、B17、B27、C01、C05 证据不足。25 项有限目标满足也不等于全部附带字段一致，不能称为全聚宽对齐。
- 未认证严格历史 PIT、完整公司行动链或任意行情覆盖；不提供持续实时 paper、真实券商接入或全市场任意回测。
- Python 策略仅供可信自有代码使用。独立进程和超时限制不构成恶意代码沙箱；服务面向本机使用。
- 为本版本选择新的控制库。旧任务依赖原源码、数据和运行时身份；不自动迁移。保留旧库及同名任务目录，用原固定环境读取。
- 行情、凭据、浏览器记录和独立验证材料不属于公开包。合成输入仅用于演示和工程测试。
- 本交付保留现有源码预览许可状态，未新增开源、商业或再分发授权。第三方引擎与数据分别适用各自许可；源码可取得与使用权是不同条件，见[许可来源与范围](docs/licensing.md)。
