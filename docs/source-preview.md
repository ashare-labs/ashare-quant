# 源码预览交付说明

## 这次交付是什么

从后端固定提交 `6099900eb309cb6941033376d5971936d5ec89be`（tree `ebafde46e13190fff5b6fb1fff7061e6b81e4d9e`）按白名单导出源码，在独立仓库建立新的根提交。没有将旧开发仓库的 Git 历史带入公开候选，也没有修改旧候选、已有任务或 quant-web。

预览版本为 `0.4.0.dev15+sourcepreview1`。它与原已审 `0.4.0.dev15` 的源码身份、wheel、依赖锁不同，原前端 pin 不适用于此版本。此整理尚未经过独立发布复核，尚未推送、打 tag、创建 Release 或发布到包索引。

## 公开内容

| 保留内容 | 用途 |
|---|---|
| `src/ashare_quant/` 的 Python 实现及必要 JSON／摘要资源 | 本地控制、接口、契约、fixture、研究实现与身份约束 |
| `src/rqprobe/` | 原生适配实现及上游源码摘要清单；无 RQAlpha 软件副本 |
| `src/ashare_quant/fixtures/` | 小型人为合成输入及手工规格 |
| README 和本目录的使用／边界说明 | 安装、演示、真实研究前置条件、版本路由及许可状态 |
| `docs/rqalpha/RQAlpha-LICENSE.txt` | 完整条件许可原文，未改写 |
| `scripts/freeze_runtime.py`、`smoke_public.py`、`smoke_http.py` | 构建时指纹生成与不执行原生引擎的可重复冒烟 |
| 公开 `tests/` | 基本契约、控制流程、fixture API 和缺失材料拒绝 |
| `pyproject.toml`、`uv.lock`、`requirements-core.txt`、`.python-version` | 安装元数据、锁定版本、供 wheel 验证的默认依赖导出与 Python 补丁版本 |

最终逐文件摘要见根目录 `PUBLIC_CONTENTS.json`；它不列自身摘要，以免循环。Git commit/tree 和外部交付回执负责固定该清单。

原树 1046 个文件中按白名单导出 122 个，再做本预览的说明和安装适配。未导出的类别包括：749 个旧文档／实验材料、34 个验收或私有打包脚本、42 个原研究／引擎验收测试、89 个封存结果证据文件、9 个嵌入或第三方 wheel，以及 1 个含个人路径的 owner release receipt。这里的文件数不是原验收测试用例数。

源码内保留的旧审阅 JSON／hash 仅描述历史绑定；缺失材料不会被修改 hash、替换成假数据或自动下载。完整旧验收仍由原隔离候选及其独审报告承担，没有将未运行的原验收计入本预览结果。

## 有意发生的变化

1. 独立版本名；默认依赖移除 vendor 路径，A数达改为固定公开 Git 提交，RQAlpha 改为可选依赖并从官方 PyPI 解析。原有第三方锁定版本保持不变。
2. 根据默认安装的实际依赖重新冻结运行时版本闭包和锁文件摘要。没有伪用旧已审 runtime hash。
3. capabilities 根据 RQAlpha 是否安装报告可用的演示模式，并标明本预览未重做原生验证；不将缺失依赖写成已启用。
4. 缺失 owner release receipt 时返回明确的 `503 M2_OWNER_RELEASE_UNAVAILABLE`。既有 M2 执行器／结果材料检查继续拒绝缺失项。
5. 只保留适用于公开 fixture 范围的测试，并增加缺失材料的拒绝检查。原任务、交易策略、账本计算和研究参数规则未改写。

## 历史独审与本次验证分开记录

原后端的独审结论为 `PASS_BOUNDED_PARAMETER_RESEARCH`，报告 SHA256 为 `1584132e6f751ee660ebac61328866bdc9438aaa7d3ce868c10414b6012ea40b`。其验证包括原源码 564 项、wheel 定向 142 项及固定范围的原生实验；这些没有在本次重跑。

原参数前端与后端组合结论为 `PASS_BOUNDED_PARAMETER_FRONTEND_AND_DEV15`，报告 SHA256 为 `e826db2202199a7a7a24cd90eca48c6a9b470fb57e595414bd6f813f74e76f9f`。该结论仅覆盖固定开发组合，明确不授予发布或部署准入。

本次源码预览只验证全新环境安装、合成 fixture CLI、临时 HTTP→独立 worker、公开小型回归集与独立 wheel 安装。具体实测结果固定在 `PUBLIC_RELEASE.json` 中。未采集新数据，未运行原生引擎，未重跑完整研究验收，未复核其他平台或长期运行。

独立 wheel 验证采用下面的安装方式。`requirements-core.txt` 来自锁文件的默认依赖导出；单独安装 wheel 时也需要它来固定传递依赖。它不包含可选原生依赖。公开 Git 依赖按完整 commit 固定，主源码安装仍推荐 `uv sync --locked`。

```sh
uv build --wheel
uv venv --python 3.12.13 .verification/wheel-env
uv pip install --python .verification/wheel-env/bin/python \
  -r requirements-core.txt \
  dist/ashare_quant-0.4.0.dev15+sourcepreview1-py3-none-any.whl
.verification/wheel-env/bin/python scripts/smoke_public.py
```

默认演示只需默认依赖；开发环境额外安装的 pytest/httpx/ruff/packaging 不参与运行时指纹闭包。维护者修改依赖时，应同步重建锁、执行 `scripts/freeze_runtime.py`、重新导出默认依赖，并重新验证 wheel；不能手工放宽旧指纹检查。

## 发布决策仍在外部

源码预览具备可复查候选后，还需要维护者确认公开文件清单和许可状态，再决定是否把这个新的根提交推至预发布分支。没有默认配置发布 remote，也没有把整理授权解释为推送授权。

“可用真实研究版”另需解决固定数据包的分发权限、可移植 receipt、封存执行器的源码构建链、独立结果 catalogue 元数据和前端的新身份绑定；参见[前置条件](research-prerequisites.md)。公开源码本身不消除这些阻塞。
