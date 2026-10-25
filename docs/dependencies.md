# 依赖的声明、锁定与安装

策略运行时模块为 `aquant`；发行包仍为 `ashare-quant`，不要安装 PyPI 上同名的第三方 `aquant` 包。详见 [入口迁移与 wheel 边界](aquant-migration.md)。

`pyproject.toml` 声明项目依赖和 core/native/dev 安装范围，`uv.lock` 是已解析版本、来源和制品哈希的唯一锁定来源。日常安装使用 `--locked`，让声明与锁不一致时直接失败；只有有意更新依赖时才运行 `uv lock`。不要手工维护另一份安装版本清单。

| 文件 | 职责及消费者 | 更新方式 |
| --- | --- | --- |
| `pyproject.toml` | 包的依赖约束、native extra、开发依赖与构建后端；uv 解析和 wheel 元数据使用 | 有意改变支持的依赖时编辑，再 `uv lock` |
| `uv.lock` | core/native/dev 共用的完整解析结果及制品哈希；sync、export、wheel 验证使用 | `uv lock`，安装和导出时仅用 `--locked` |
| `.python-version` | uv 选择的 Python 补丁版本，与运行时快照中的 `python` 一致 | 有意更新 Python 时修改，再重建和验证两种快照 |
| `src/ashare_quant/runtime-lock.json` | 核心运行时版本快照；产品验证实际安装版本及恢复身份 | 锁定环境运行 `python scripts/freeze_runtime.py` |
| `src/ashare_quant/native-runtime-lock.json` | 原生运行时版本快照；native 执行准入使用 | 同一环境运行 `python scripts/freeze_runtime.py --native` |
| `src/ashare_quant/lock.sha256` | `uv.lock` 的摘要；运行清单及恢复准入使用 | 上述生成器同时更新，不能手填 |
| `src/ashare_quant/providers/data-runtime.json` | 唯一 AD 的提交、wheel 和安装代码身份；不是另一套解析结果 | 按 [开发指南](development.md) 配对并运行 pin_data_runtime.py |

两个 runtime JSON 是从锁定安装环境得到的验收快照，不是安装输入。生成器核对安装版本等于 uv.lock 后才输出；加 `--check` 会只读验证快照，漂移时失败。core/native 快照不可合并：核心安装不要求 RQAlpha，原生执行需要验证完整 native 依赖。native extra 的显式约束还进入 wheel 元数据，限定已支持的原生环境，保留这些声明。

原根目录的 `requirements-core.txt`、`requirements-native.txt` 只是 uv 导出的副本，没有独立消费者或手工内容，现不再跟踪或随 sdist 分发。需要 pip 格式时临时从同一锁导出，避免多份文件漂移。

## 从源码或 sdist 安装

需要 uv 和 `.python-version` 指定的 Python 3.12 补丁版本，先准备与 owner pin 相同的 AD wheel。以下命令均从项目根目录执行：

设置 `AQ_WHEEL` 为本次构建的实际 wheel 路径。

```sh
python scripts/stage_owner.py --source /path/to/ashare-data
uv sync --locked --no-dev
```

这是 core 安装。执行原生回测时增加 `--extra native`；开发及运行 pytest 时去掉 `--no-dev`。默认开发依赖包含 pytest、httpx 和 Ruff，不用于业务运行。构建后端按 `[build-system]` 单独固定，不进入运行时快照。

## 离线安装与按需导出

在已准备对应 Python、依赖和构建工具的 uv 缓存及配对 AD wheel 后，同一安装命令加 `--offline` 即可；缺包应失败，不能临时换版本。用 `UV_CACHE_DIR` 指定交付缓存。sdist 带 uv.lock 和生成脚本，AD wheel 在 local-dependencies 单独准备，不携带真实数据。

需要 hash 校验的 pip/uv pip 输入时，用明确的临时目录生成：

```sh
mkdir -p .verification
uv export --locked --offline --no-dev --no-emit-project --output-file .verification/runtime-core.txt
uv export --locked --offline --no-dev --extra native --no-emit-project --output-file .verification/runtime-native.txt
uv venv /path/to/new-venv --offline
uv pip install --python /path/to/new-venv/bin/python --require-hashes -r .verification/runtime-native.txt --offline
uv pip install --python /path/to/new-venv/bin/python --no-deps "$AQ_WHEEL" --offline
```

导出保留包哈希，且不安装 AQ 项目本身；最后一步再装已构建的 AQ wheel。导出的 AD wheel 路径相对项目根目录，因此这些安装命令也从项目根运行。导出文件是临时交付物，不提交；core/native 只生成所需的一份即可。`scripts/verify_wheel.py` 已使用这条锁定导出链，在新环境安装时同时核对两种运行时快照和安装源码身份。
