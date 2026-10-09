# 真实有限研究的前置条件

本源码预览保留了有限参数研究的实现，但**没有可供新用户下载并直接运行的完整研究包**。下面列出材料、获取状态和现有接口，避免把本机已经具备的条件写成公开安装能力。

## 已审范围

只包含 `600000.XSHG`、日频、`previous_close_band.v1` 和三个固定 2020 年窗口：

| 窗口 | 执行日 | 暖启动日 | 最后后继日 |
|---|---|---|---|
| m2a | 01-03、01-06 | 01-02 | 01-07 |
| w1 | 01-03 至 01-16，共 10 个交易日 | 01-02 | 01-17 |
| w2 | 01-06 至 01-17，共 10 个交易日 | 01-03 | 01-20 |

资金为 `1000.00` 至 `10000.00`，目标股数为 100 或 200，阈值满足 `0 < enter_below < exit_above <= 1000000`，十进制参数使用字符串且最多两位小数。决策使用前一交易日收盘价；匹配为带显式假设的事后日线模型。仍非严格 PIT、全市场数据、任意策略、分钟研究、paper/live 或聚宽等价实现。

## 外部材料与获取状态

| 材料 | 固定身份／取得方式 | 当前交付状态 |
|---|---|---|
| A数达源码 | 公开提交 `a4086b188c3023fded59731ba154fd44ee7cde37`，默认安装已锁定 | 可联网安装，仅此不构成原 M2 运行准入 |
| 原已审 owner wheel | 0.7.0.dev1，SHA256 `95078db7bdb8141159e5d9e7f9fba33eda3c14e1957e60b2ce0c034026ff2a45` | 未捆绑；需要维护者授权交付。公开源码重建 wheel 的元数据／摘要不同，不能替换旧 pin |
| 固定 M2 来源包 | manifest SHA256 `5d2a2ba8690747028325cba9ebab0c0202d508c4a163d5972b728c44e6b4c436`；74 个来源文件及清单 | 无公开下载地址，数据再分发权限未确认；应由维护者确认权限后提供精确包，不能重新抓取近似数据代替 |
| owner release receipt | SHA256 `7ae85fbd491bee15028b11bc6b037a246643f8d6046d57c62419c9571eba44e0` | 原件含个人路径，已从公开树移除；不能改写后冒称原摘要，便携替代需要重新绑定 |
| 封存参数执行器 | `0.4.0.dev15+exec1` wheel SHA256 `8a66705a78a74a875fd0fbbdb275a11ff9e7a83a72d4968fd3f2c9211815fe9b` | 未分发；原构建依赖旧封存执行器，需补可重复源码构建链及独立身份复核 |
| 审定结果／catalogue 材料 | 原 dev15 固定结果目录及其闭包 | 包含研究轨迹和检查点，已移除；任务 catalogue 目前依赖它们，需先拆分可公开的元数据与私有实验材料 |
| RQAlpha | 6.4.1，可选 `native` extra 从官方 PyPI 安装 | 仅依赖可取得，使用资格见完整条件许可；本轮未运行 |

数据来源包括 BaoStock 的固定日线、完整源日历及停牌／ST／preclose 补字段，还包括相关公司公告、交易所规则及既有独审材料。来源说明与公开读取契约见 [A数达固定提交的 M2 API 文档](https://github.com/ashare-labs/ashare-data/blob/a4086b188c3023fded59731ba154fd44ee7cde37/docs/m2-api.md) 和 [BaoStock 来源说明](https://github.com/ashare-labs/ashare-data/blob/a4086b188c3023fded59731ba154fd44ee7cde37/docs/baostock-source.md)。其中原开发文档的“待审”是冻结时状态；后续独审边界见本仓库交付说明。

## 取得授权的固定包后，现有导入接口是什么

以下演示的是 A数达数据导入接口，不代表本源码预览已能执行真实研究。自行选择包路径和新的 Store 路径；不要覆盖现有 Store：

```sh
export M2_INPUT_BUNDLE="$PWD/input-bundle"
export M2_OWNER_STORE="$PWD/.state/m2-owner"
uv run --locked --no-dev python - <<'PY'
import os
from pathlib import Path
from ashare_data import Store

store_path = Path(os.environ["M2_OWNER_STORE"])
if store_path.exists():
    raise SystemExit("请选择全新的 Store 目录")
store = Store.init(store_path)
dataset_id = store.import_m2(Path(os.environ["M2_INPUT_BUNDLE"]))
print(dataset_id)
for profile in store.m2_profiles(dataset_id):
    print(profile.data.decode("utf-8"))
PY
```

导入只核验固定清单及原始字节，不联网、不修补输入、不按目录名推定可信。来源包不符会拒绝。相同接口也有 CLI：先初始化 Store，再使用 `ashare-data --store PATH m2 import --request REQUEST.json`，请求为 `{"directory":"包路径"}`。

完整已审服务还需要 `ASHARE_QUANT_M2_STORE` 指向这个 Store，并保留精确版本、执行器、结果 catalogue 和所有条件假设。**仅设置环境变量、仅安装 RQAlpha 或仅导入数据均不足以解除本预览的缺失材料拒绝**。本轮不自动复制或迁移旧任务、Store 或数据库。

## 下一个可用研究版本还需完成

先确定上述数据／封存材料的可交付权限和来源，再完成无个人路径的 release receipt、可重复执行器构建、独立 catalogue 元数据和新的身份绑定。完成后在无私有 task 文件的新环境中验证一次真实有限任务、结果读取与必要恢复路径，并重新核对前端 pin 和三个服务路由。原独审通过证明固定开发组合；它不能代替这一新的便携交付验证。
