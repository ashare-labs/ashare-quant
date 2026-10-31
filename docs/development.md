# 开发、依赖与许可

本篇整合 `ashare-quant` 的依赖锁定与运行时快照流程、契约生成与 wheel 隔离验证、软件与数据许可说明，以及正式后端的历史退役接口对照表。

## 依赖声明、锁定与运行时快照

项目依赖由 `pyproject.toml` 与唯一锁文件 `uv.lock` 管理。所有安装与导出操作均加 `--locked`，确保环境与锁文件严格一致：

| 文件 | 职责与更新方式 |
| --- | --- |
| `pyproject.toml` / `uv.lock` | 声明 `core`、`native`、`factor-evaluation`、`mcp`、`dev` 依赖及完整制品哈希；有意变更依赖时运行 `uv lock` |
| `.python-version` | 锁定 Python 3.12 补丁版本，与运行时快照中的 `python` 字段一致 |
| `src/ashare_quant/providers/data-runtime.json` | 绑定配对 `ashare-data` 的 commit、文件清单与 wheel SHA-256；由 `scripts/stage_owner.py` 与 `scripts/pin_data_runtime.py` 生成 |
| `src/ashare_quant/runtime-lock.json` | 核心运行时已安装版本快照；由 `python scripts/freeze_runtime.py` 生成 |
| `src/ashare_quant/native-runtime-lock.json` | 含 RQAlpha 的原生运行时版本快照；由 `python scripts/freeze_runtime.py --native` 生成 |
| `src/ashare_quant/lock.sha256` | `uv.lock` 的 SHA-256 摘要，随 `freeze_runtime.py` 自动更新 |

### 配对更新 `ashare-data` 与冻结快照

当上游 `ashare-data` 代码或版本更新时，在仓库根目录依次执行：

```sh
# 1. 从 ashare-data 源码构建候选 wheel，更新锁并安装到当前环境
python scripts/stage_owner.py --source /path/to/ashare-data
uv lock
uv sync --locked --extra native --extra factor-evaluation --extra mcp

# 2. 写入 data-runtime.json 并刷新运行时快照
python scripts/pin_data_runtime.py \
  --wheel local-dependencies/ashare_data-0.10.0.dev98-py3-none-any.whl \
  --commit <AD_COMMIT> --tree <AD_TREE>
python scripts/freeze_runtime.py
python scripts/freeze_runtime.py --native
```

> **关于 `data-runtime.json` 固定字段的说明**：`pin_data_runtime.py` 生成的 `portable_owner_version: "0.10.0.dev1"` 为上游 Portable 研究包协议世代号（非包版本）；`distribution_status: "unpublished_candidate"`、`contract_review.status: "not_reviewed"` 与 `execution_permission: false`（以及 `docs/contracts/identity.json` 的 `independent_review_status: "not_reviewed"`）是工具脚本的出厂静态标记，表示代码包配对绑定本身不自动继承历史独审结论或授予实盘/新交易日执行权限。

### 离线安装与按需导出 `requirements.txt`

需要在新虚拟环境中离线安装时，可从 `uv.lock` 临时导出带哈希校验的依赖清单：

```sh
mkdir -p .verification
uv export --locked --offline --no-dev --extra native --no-emit-project --output-file .verification/runtime-native.txt
uv venv /path/to/new-venv --offline
uv pip install --python /path/to/new-venv/bin/python --require-hashes -r .verification/runtime-native.txt --offline
uv pip install --python /path/to/new-venv/bin/python --no-deps "$AQ_WHEEL" --offline
```

---

## 日常校验、契约生成与 Wheel 验证

代码或接口变更后，依次执行以下检查：

```sh
python scripts/generate_contracts.py --output docs/contracts
python scripts/export_mcp.py
python scripts/check_structure.py
python scripts/freeze_runtime.py --check
python scripts/freeze_runtime.py --native --check
ruff check src scripts tests
python scripts/run_tests.py -q
```

构建 wheel 并在隔离环境中验证安装身份、原生执行与契约一致性：

```sh
uv build
python scripts/verify_wheel.py dist/ashare_quant-0.12.0.dev32-py3-none-any.whl \
  --extra factor-evaluation --extra mcp --output .verification/wheel-check
```

---

## 软件许可与第三方材料说明

- **项目自有代码**：权利人尚未为 `ashare-quant` 与配对的 `ashare-data` 自有源码指定开源许可证（未附 MIT/Apache-2.0 等自有代码开源授权）；未经权利人明确授权，不得进行商业分发或再授权。
- **RQAlpha 原生依赖（`native` extra）**：锁定使用 `rqalpha==6.4.1`，其完整许可条款保存在 [`docs/rqalpha/RQAlpha-LICENSE.txt`](rqalpha/RQAlpha-LICENSE.txt)。该许可对非商业个人/教育研究与商业/组织用途作出区分，使用者应按完整条款确认自身使用资格。
- **其他运行依赖**：`empyrical-reloaded==0.7.0`（Apache-2.0）、`alphalens-reloaded==0.5.0`、`fastapi`、`pydantic`、`duckdb` 等依赖由 `uv.lock` 从官方 PyPI 安装，分别适用各自随包许可证。

---

## 附录：历史退役接口与新入口对照表

早期预设七算法组合平台、固定日频窗口（`two_day/w1/w2`）及合成演练写接口已从产品路由表与 OpenAPI 契约中移除（请求返回 `404` 或 `405`），统一收敛至自定义 Python 工作台与只读历史接口：

| 已退役接口 / 命令 | 现行替代入口 |
| --- | --- |
| `/api/v1/portfolio/**`（预设算法目录、注册、预检、任务创建与控制） | 新策略提交使用 `/api/v1/python/jobs`；历史组合结果通过 `GET /api/v1/history/jobs?family=portfolio` 只读查询 |
| `/api/v1/tasks/daily/**`（固定窗口目录、规则策略注册、数据绑定与任务创建） | 日频策略统一通过 `POST /api/v1/python/jobs`（`frequency="1d"`）提交；历史记录通过 `GET /api/v1/history/jobs?family=daily` 查询 |
| `GET /api/v1/scenarios`、`/rqalpha/scenarios`、`POST /api/v1/strategies`、`/runs`、`/data/admission` | 演练场景与合成数据注入已移除；使用 `/api/v1/product` 正式数据发布与覆盖查询，旧 `runs` 保持只读 |
| `POST /api/v1/paper/sessions` 及 `fork`、`ingest`、`advance`、`work`、`decisions`、`closes`、`pause`、`resume` | `/api/v1/paper` 仅提供已有会话、视图与报告的只读查询 |
| 旧 CLI `demo`、`scenarios`、`strategy-create`、`run-create`、`task-create`、`portfolio` | 使用 `python-create`、`task-worker`、`task-service`、`history`、`history-strategies` |
