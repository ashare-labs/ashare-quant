# 开发与打包

先按 [README](../README.md)构建固定 A数达依赖。在项目根目录使用锁定的 Python 和依赖，不通过跳过校验消除安装错误。

## 验证

```sh
uv sync --locked
.venv/bin/python scripts/smoke_public.py
.venv/bin/python scripts/smoke_http.py
.venv/bin/pytest -q
```

两份 smoke 分别验证合成 CLI，以及临时本机 HTTP 与独立 worker；完成后停止服务。安装可选原生引擎后可运行原生合成测试：

```sh
uv sync --locked --extra native
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 .venv/bin/pytest -q
```

缺外置输入的测试明确 SKIP，不计为通过。相关变量为 ASHARE_QUANT_TEST_M2_COMPONENTS、ASHARE_QUANT_TEST_M2_PRODUCT、ASHARE_QUANT_TEST_V3_DATABASE、ASHARE_QUANT_TEST_V4_DATABASE、ASHARE_QUANT_TEST_V5_DATABASE、ASHARE_QUANT_TEST_DIAGNOSTICS_CONFIG。单项研究还要求固定 reader 与绑定。旧库测试使用关闭后的副本与同名任务目录，不自动采集。

## 发行文件

```sh
uv build
```

生成 `dist/ashare_quant-0.11.0.dev14-py3-none-any.whl` 和对应 `.tar.gz`。wheel 包含两个实现包；源码包使用 pyproject.toml 的明确白名单，收录实现、示例、测试、当前文档、锁文件、必要脚本与完整第三方许可。许可说明通过标准 `License-File` 元数据声明，随 wheel 安装到 `.dist-info/licenses/docs/`；其中的相对链接仍可使用，没有填写未经权利人选择的 SPDX 许可证。

独立证据保留在开发树原位置；过时交接、重复计划和失效入口以 Git 删除，可从基线恢复，没有移入新的档案目录。开发 checkout 及 Git 历史不是公开交付包，不应直接公开整个开发仓库。

发行包排除旧运行报告、`.verification`、本地 owner wheel、数据库、行情、凭据、浏览器记录、日志和 checkpoint。检查实际压缩包内容；`.gitignore` 不会自动移除已跟踪材料。公开示例仅用人工合成输入。

## 独立 wheel 安装

依赖导出仍引用项目根目录准备的固定 owner wheel：

```sh
uv venv --python 3.12.13 .verification/wheel-env
uv pip install --python .verification/wheel-env/bin/python -r requirements-core.txt dist/ashare_quant-0.11.0.dev14-py3-none-any.whl
.verification/wheel-env/bin/python scripts/smoke_public.py
```

原生研究用 requirements-native.txt。依赖变更需重新核对 pin、锁、运行时摘要和测试，不能只改版本；本次文档与打包整理保持原运行依赖。

## 发布准备边界

固定 A数达 `0.8.0.dev2` 已能从其公开 GitHub 仓库按完整提交获取，并构建出固定摘要的 wheel。README 给出这条取源路径；不需要另发私有开发依赖、不切换 owner 版本，也不随 AQ 捆绑 AD 二进制。

AQ 及固定 AD 的既有说明都没有授予额外开源、商业或再分发许可。当前准备的是保留原声明的源码预览，技术安装与合成示例可复现；不能据此宣传第三方已有自由使用权。没有指定开源许可证不被解释为“项目权利人不能准备源码预览”。若发行目标包含第三方使用、修改或再分发，最小待决策项是由权利人一次明确 AQ 与固定 AD 所需的授权范围；本次没有代选新许可证。来源见[许可说明](licensing.md)。

构建和本机测试不包含 push、tag、Release、包索引上传或部署，也不更改 38 项研究结论。
