# A宽（ashare-quant）

## 项目简介

A宽是面向本机使用的 A 股量化研究后端，提供策略注册、持久任务、CLI、HTTP API、运行记录和结果查询。真实研究的数据由 A数达提供，交易账户由 RQAlpha 维护。仓库自带合成示例，便于先了解任务和结果的使用方式。

当前发行包版本为 **0.11.0.dev18**。已审源码的服务身份标签仍为 `0.11.0.dev15`，须与源码摘要共同核对。日频任务支持固定证券和窗口内的可信 Python 策略及 JSON 规则；分钟事件、跨日持仓、费用和公司行动另有固定研究入口。这些实验能力不代表服务支持任意股票、日期或完整聚宽 API。

**固定真实历史输入下的连续模拟已通过独立验收。** 等权、动量和均线过滤策略共用 RQAlpha 原生账本，逐日接收数据、续接账户、校验风险和恢复中断。独立验收重现52个账户交易日、25笔唯一成交，并验证分红只入账一次及未来输入追加后的旧日幂等。2024必要字段缺证的订单继续拒绝；这不代表真实在线跨交易日运行或全聚宽对齐。

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

历史连续模拟通过独立读取器消费固定 AD `dc0198bc` / `0.9.1.dev13` 的已授权封存输入；它不替换上述原生环境中的 `0.8.0.dev2`。数据、执行收据和浏览器证据保留在本地运行目录，不随源码分发。输入绑定与模型选择见[AD逐日交付](docs/paper-execution.md)。

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

查看已在本机准备的历史连续模拟账户，在后端终端运行：

```sh
cd ~/Workspace/ashare-quant
.venv/bin/python -B -m ashare_quant.paper --root .state/paper/store serve --port 18818
```

在独立前端终端运行：

```sh
cd ~/Workspace/quant-web
FRONTEND_PORT=41818 PAPER_BACKEND_PORT=18818 npm run dev
```

打开 <http://127.0.0.1:41818/#/paper>。两个服务只监听本机，分别按 `Ctrl+C` 停止。初次使用没有绑定输入时会显示空会话，先按下方指南准备数据；不会自动取数或创建订单。本机已验收账户的来源、路径和完整版本映射保存在私有 `.state/paper/entrypoint.json`（如已配置）。

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

**真实分钟 Python 策略。** [TDX 两股研究例子](examples/tdx_minute_research/README.md)提供两股两日的 924 条真实原生分钟、共享资金与 T+1 策略、可复现订单/成交/账户结果，以及数据取得方法。它显式接受有界研究假设，原严格分钟题和聚宽对照状态保持不变。

**完整分钟会话对照。** [R7 的本机入口](docs/jq-minute-r7-integration.md)使用 AD 的完整分钟、预热和收盘估值，复放三份已封存真实脚本。仅限两股两日、显式 non-PIT 事后研究，未知容量与撮合政策仍单列；真实材料保存在本地忽略目录。

**诊断与开发。** `source-diagnostics-capabilities` 查看配置；只读检查需独立固定的 reader 环境及已有数据，见[数据诊断](docs/diagnostics.md)。测试和发行包构建见[开发与打包](docs/development.md)。

**历史连续模拟。** `python -m ashare_quant.paper` 及独立 quant-web `#/paper` 已完成封存历史逐日消费的独立验收。先接收固定下一日数据，再推进原生账户；同日重复请求复用原报告，中断后恢复同一冻结日，修改策略、风险或数据须新建账户。准备自己的已授权输入和空账户、查看公司行动和2024逐单门禁，见[AD逐日交付与原生分红](docs/paper-execution.md)。独立浏览器联验不冒充人类亲自点击；接口的 `continuous_paper_accepted=false` 继续表示真实在线连续模拟尚未通过。

**封存响应回归。** [真实响应回放](docs/response-replay.md)提供单入口：复用 A数达采集/准入和 RQAlpha 原生账户路径，在隔离身份中重放原文、收讫时间、失败与恢复。派生故障单独标注，不写当前模拟账户。

## 限制与说明

- 日频任务服务仍限 `600000.XSHG`、`m2a/w1/w2` 固定窗口及连续前缀、整百股目标（支持清仓为 0，资金不足由原生引擎拒单）。实验入口不自动扩展服务范围。
- B25、B26、C02、C03 保留实质差异；B12、B14、B17、B27、C01、C05 证据不足。25 项有限目标满足也不等于全部附带字段一致，不能称为全聚宽对齐。
- 历史连续模拟使用明确研究模型；2024四只沪市证券的232证券日、696个ST/涨跌停字段仍未知，实际订单需要这些字段时拒绝。观察告警和固定历史影响统计不授予订单许可。
- 格力样本保留JQ经济差878.92元：AQ毛权益3500、JQ日志净额2621.08；其中655.27是毛净额推得的扣减，没有独立税款流水证明，不能据此调整源数据或账本。
- 未知历史源送达与真实分红日内到账时刻；未认证严格历史 PIT、完整公司行动链或任意行情覆盖。未通过真实在线跨交易日持续模拟，不提供真实券商接入或全市场任意回测。
- Python 策略仅供可信自有代码使用。独立进程和超时限制不构成恶意代码沙箱；服务面向本机使用。
- 为本版本选择新的控制库。旧任务依赖原源码、数据和运行时身份；不自动迁移。保留旧库及同名任务目录，用原固定环境读取。
- 行情、凭据、浏览器记录和独立验证材料不属于公开包。合成输入仅用于演示和工程测试。
- 本交付保留现有源码预览许可状态，未新增开源、商业或再分发授权。第三方引擎与数据分别适用各自许可；源码可取得与使用权是不同条件，见[许可来源与范围](docs/licensing.md)。
