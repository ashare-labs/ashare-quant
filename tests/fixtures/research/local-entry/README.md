# 本机统一入口可移植模板

本目录提供本机统一入口启动器 `data`、入口脚本 `entry.py`、已结算模拟账户只读扩展 `account_entry.py` 以及配套配置模板（`deployment.example.json`、`catalog.example.json`）。所有入口直接调用虚拟环境中已安装的配对 `ashare-data` / `ashare-quant` 包。

## 初始化与配置

初始化新的本机入口目录时：

1. 将 `data`、`entry.py` 与 `account_entry.py` 复制到目标入口目录，并准备配对虚拟环境。
2. 将 `deployment.example.json` 与 `catalog.example.json` 复制为 `deployment.json` 与 `catalog.json`。
3. 将模板中的 `REPLACE_WITH_REVIEWED_*` 占位符替换为真实资产的 SHA256 摘要，并通过 `data_root` 与相对路径引用既有行情、财务、因子与参考数据。

在已有入口目录中增量接入新分钟源或目录项时，仅合并新增的 `minute_sources` 与对应 `catalog.json` 数据集条目，保留既有资产登记与运行状态路径。

常用查询示例：

```sh
./data minute-sources
./data minute --source neigezhu --security 600000.XSHG \
  --start 2026-06-03T09:40:00+08:00 --end 2026-06-03T09:42:00+08:00
./data minute --security 600000.XSHG --day 2026-10-09
```

`list` / `show` 用于展示 `catalog.json` 中的数据集元数据；`minute-sources` 从 `deployment.json` 解析分钟数据源绑定。配对 wheel 安装、初始化命令与 API 请求参数详见 [数据、启动与恢复](../../../../docs/data-runtime.md)。

## 已结算模拟账户只读登记

`account_entry.py` 是可选的本机部署扩展，用于从 `state/online/registry.json`（schema：`ashare-online-account-registry.v1`）只读加载已完成运行时授权的 `OnlineService` 结算账本。

每个登记项包含 `account_id`、`label`、`path`（与 `account_id` 同名）、`identity`、`head`、`runtime_pin_sha256`、`review_sha256` 以及 `files`（记录账户目录下除 `.lock` 外全部文件的 `bytes` 与 `sha256`）。在 `catalog.json` 中配置 `account_reader` 后，可通过以下入口查询：

```sh
./data accounts
./data account ACCOUNT_ID
./data api
./data web
```

本机 `./data api` 会额外挂载只读路由 `GET /api/v1/online-accounts` 与 `GET /api/v1/online-accounts/{account_id}`（契约版本 `local-online-accounts.v1`），供 `quant-web` 的 `#/accounts` 页面展示现金、净值、持仓、委托、成交与迁移身份。备份恢复时需完整保留 `state/online` 目录、登记文件与对应的 `runtime-bundle`。
