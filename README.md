# A宽（ashare-quant）源码预览

`0.4.0.dev15+sourcepreview1` 是从已审 dev15 后端整理的独立源码快照，提供本地任务控制、HTTP API、持久化状态及合成 fixture 演示。新机器可以安装并运行下面的演示；其中的价格、成交和账户变化均为人为构造，**不是真实行情回测，也没有启动 RQAlpha**。

真实的有限参数研究实现仍保留在源码中，但所需固定行情包、封存执行器、历史结果材料和原始 owner release receipt 没有公开交付。源码预览不能直接运行这类任务，也不能替换 quant-web 已审的三个后端服务。详见[真实研究前置条件](docs/research-prerequisites.md)和[版本路由](docs/service-routing.md)。

## 安装

在包含本 README 的源码目录中操作。需要 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 **Python 3.12.13**；`.python-version` 和运行时指纹固定此补丁版本。本轮验证平台为 macOS arm64；其他 POSIX 平台尚未验证，原生 Windows 不在范围内（源码使用 `fcntl`）。

```sh
uv python install 3.12.13
uv sync --locked --no-dev
```

首次安装需要联网访问官方 PyPI 和 GitHub，无须预置缓存，不要加 `--offline`。`ashare-data` 从公开源码的固定提交 `a4086b188c3023fded59731ba154fd44ee7cde37` 构建，其余依赖由 `uv.lock` 锁定。没有引用发布者电脑中的路径，也没有随仓库捆绑第三方 wheel。

RQAlpha 默认不安装。只有确认符合其[完整条件许可](docs/rqalpha/RQAlpha-LICENSE.txt)且需要研究原生适配代码时，才使用 `uv sync --locked --extra native`。这个选项仅安装依赖，不补齐真实研究材料；本次没有重新执行原生引擎验证。自有代码的许可状态及其他依赖边界见[许可说明](docs/licensing.md)。

## 运行合成演示

```sh
uv run --locked --no-dev ashare-quant --db .state/demo.sqlite3 demo
```

成功输出包括 `state.run.status="succeeded"`、`state.is_test_double=true`、`state.account.cash="9000.00"`，以及一条合成成交。运行清单的 `real_strategy_executed=false`。默认初始资金为 `10000.00`，此结果只检查预先编写的 fixture 经过任务、worker、数据库和读取接口后的状态。

同一数据库与默认幂等键会复用同一任务。需要新演示时，使用另一个 `--db` 路径或 `demo --key another-demo`。这些数据库是本地运行输出，不属于源码交付。

## 启动本地 API

```sh
uv run --locked --no-dev ashare-quant --db .state/api.sqlite3 serve --port 8805
```

打开 <http://127.0.0.1:8805/api/v1/docs> 查看接口；健康检查为 `/api/v1/health`。服务仅绑定本机，按 Ctrl-C 停止。API 提交任务后，需要另一个终端在同一源码目录、同一数据库上执行 worker：

```sh
uv run --locked --no-dev ashare-quant --db .state/api.sqlite3 worker --once
```

公开预览的 M2 catalogue 会明确返回 `M2_JOB_ARTIFACT_UNAVAILABLE`，不会补造结果或自动取数。旧 D0/BR1 数据接口保留其历史 pin，与本预览的数据依赖不同；不要将这些接口视为新安装后的真实数据入口。

## 最小验证

```sh
uv sync --locked
uv run --locked python scripts/smoke_public.py
uv run --locked python scripts/smoke_http.py
uv run --locked pytest -q
```

这些检查仅使用临时目录和小型合成 fixture，不采集行情，不启动原生引擎。HTTP 冒烟只在临时本机端口启动自己的服务，结束后停止。公开测试范围为基本契约、任务控制、fixture API 和缺失研究材料的明确拒绝；原来依赖私有研究材料的完整验收套件未随此快照发布，不能把这里的测试数量当成 dev15 全验收。

构建本地 wheel 可使用 `uv build --wheel`。构建产物不是先前独审的 dev15 wheel；公开预览有独立版本、依赖锁和源码身份。安装结果、导出清单、历史独审边界及尚未满足的发布条件见[交付说明](docs/source-preview.md)。
