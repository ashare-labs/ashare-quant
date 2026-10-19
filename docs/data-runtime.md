# 数据、启动与恢复

所有正式入口选择同一份 `ashare-deployment.v1` 清单。优先级是 `--deployment`、`ASHARE_DEPLOYMENT`、`ASHARE_DATA_ROOT/deployment.json`。清单里的相对路径以 `data_root` 为准，`data_root` 又相对清单所在目录。正式行情、财务/因子、固定评测、AQ 任务账户、临时文件和受保护的外部环境分别登记为资产；登记不代表可以删除。

## 使用已有正式数据

行情查询的最小清单如下。`main` 指向已有 `ashare-data-local`，发布 Store 和运行状态放在另一目录，不复制主库。普通回测还需配置下述参考集合。

```json
{
  "schema": "ashare-deployment.v1",
  "data_root": ".",
  "assets": {
    "main": {"kind": "local_daily", "path": "/data/ashare-data-local", "role": "market", "retention": "preserve"},
    "releases": {"kind": "store", "path": "ad/releases", "role": "publication", "retention": "preserve"}
  },
  "profiles": {
    "product": {
      "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
      "control_db": "aq/product/control.sqlite3", "paper_root": "aq/product/paper",
      "api_port": 18839, "paper_port": 18818, "web_port": 41839,
      "default_dataset_id": null
    }
  }
}
```

`owner_store_asset` 可为固定评测选择独立 Store；默认使用发布 Store。`required_assets` 列出本配置必需的库外输入、运行安装包等资产名称。不能把临时目录或受保护的旧环境列为必需输入。其他配置的资产和受保护外部资产仅登记，不随正式状态包复制。

普通回测在资产中登记 `daily_research`、`daily_execution` 和 `corporate_history` 包，每项包含 `path`、相对包目录的 `binding`（通常为 `binding.json`）和该绑定文件的 `binding_sha256`。在 profile 中添加 `reference_sets`，例如 `{"daily":{"kind":"daily_execution","research_asset":"research","execution_asset":"execution","corporate_history_asset":"corporate"}}`，并设置 `default_reference_set="daily"`。另一种集合用 `kind="historical_market"`、`market_asset` 和 `corporate_history_asset`。配置只说明来源可选择；所选证券和日期是否就绪，由 coverage 逐项检查。接口、来源范围和研究假设见[组合回测](portfolio.md)。

## 检查和启动

```sh
python -m ashare_quant.deployment --deployment /data/product/deployment.json doctor
python -m ashare_quant.deployment --deployment /data/product/deployment.json init
python -m ashare_quant.deployment --deployment /data/product/deployment.json commands
```

`doctor` 不建目录、不建库，分别报告数据库结构、AD 身份、原生依赖和缺失资产。为避免在源目录创建 WAL 辅助文件，结构检查只读数据库主文件；尚未 checkpoint 的新建表会明确显示未就绪。`init` 是显式创建候选运行状态的入口，源目录缺少有效 `daily.sqlite3` 时先拒绝，不产生 AQ 状态。

在各终端以前台方式运行：

```sh
python -m ashare_quant.deployment --deployment /data/product/deployment.json api
python -m ashare_quant.deployment --deployment /data/product/deployment.json task-worker
python -m ashare_quant.deployment --deployment /data/product/deployment.json paper
```

API 和任务 worker 共用同一 `control_db`，paper 使用同一清单的 `paper_root`。默认端口分别为 `18839`、`18818`。`commands` 输出 Web 所需 `FRONTEND_PORT=41839`、`ASHARE_API_URL` 和 `ASHARE_PAPER_URL`，在 quant-web 目录按其说明启动。命令不安装自启，不连接券商，也不采集数据。

旧 `ashare-quant` / `python -m ashare_quant.cli` 命令选择清单后也使用同一状态、端口和完整备份入口。显式 `--db` 与清单冲突时拒绝；未选清单时，涉及状态的旧命令必须明确提供 `--db` 或 `ASHARE_QUANT_DB`，不会在当前目录另建默认库。

Web 的正式数据页按真实证券目录搜索、选择日期及参考集合，先检查覆盖再发布，随后进入普通组合回测。它保留真实来源、交易状态、派生价限与独立公司行动计划，需确认页面展示的回溯研究模型。缺证会明确拒绝；用户可另外选择 `price_only_no_limits` 价格诊断，但它不承担经济回测语义，已有公司行动计划也不能在任务中被剥离。缺行情或预热数据始终拒绝执行。

## 发布繁忙时重试

常规启动使用单个本机 API。多个 API 进程共享同一发布 Store 时，发布写入会串行，但结果读取仍可能与另一请求的 SQLite 提交重叠。Store 的 SQLite 连接不等待锁；短暂的 `SQLITE_BUSY` 会使发布接口返回 `503 PRODUCT_DATA_UNAVAILABLE`，响应带 `Retry-After: 2` 和 `Cache-Control: no-store`。这不表示主行情缺失，也不保证每个并发请求第一次都成功；跨机器或网络文件系统共享 Store 未获支持。

收到该响应后等待至少 2 秒，在来源未改变的情况下重试完全相同的证券、日期、来源、模式和参考集合。页面保留选择，但清除覆盖结果和诊断确认；普通模式须重新检查覆盖，诊断模式须重新读取行情并确认，然后再发布。页面不会自动重试或创建回测任务。客户端应限制重试次数；持续失败时检查服务异常链和所选目录，不能把所有 `PRODUCT_DATA_UNAVAILABLE` 都当作锁忙，因为权限、磁盘等错误也使用此码。

失败可能发生在目录提交前，也可能发生在 AD 已提交、AQ 尚未读回时。因此不能仅凭 503 判定“什么也没写”，也不要删除输出或改名重新发布。相同源字节、参考绑定与请求会重用同一内容身份，重复请求不会增加相同数据集或自动提交账户任务；若来源更新，需重新核对返回的来源和内容身份。本行为不承诺多进程部署的吞吐或延迟。

## 保存运行状态

停止本配置的 API、worker、paper，再执行：

```sh
python -m ashare_quant.deployment --deployment /data/product/deployment.json backup /backup/product-state
python -m ashare_quant.deployment verify /backup/product-state --expected-sha256 RETAINED_MANIFEST_SHA256
```

返回值给出 `manifest_sha256`，应与备份分开保存。备份目录包含 AQ 数据库一致性快照、`.tasks`、`engines`、`inputs`、检查点/回执、完整 paper 目录、已发布 Store、选用 owner Store、选中参考集合的小型来源包、`required_assets`、原始清单和运行源码/依赖身份。显式必需的 `financial`、`factor` 或其他库外输入都会保存；若把整个 `local_daily` 主库列为必需，则必须显式选择下述整库备份，否则提前拒绝。输入和回执保留原字节，包括原始来源路径字符串。

备份持有配置锁、既有文件写锁和状态数据库写保留锁；有运行进程、活动/未核对的任务、缺失或变化的检查点、变化的库外文件时拒绝。待执行任务可以保存。源文件在复制前后做清单和 hash 检查；不能以单个 SQLite 成功快照代替整个目录的一致性。未选择清单、仅显式选择 `--db` 的旧 `ashare-quant backup` 仍只是 SQLite 文件快照，不能用于完整恢复。

状态包不包含完整正式行情大库。它能恢复已有冻结任务、账户和成交，继续已有任务；在新机器搜索或发布新行情，还需可用主库或下述源资产包。

## 备份正式源资产

```sh
python -m ashare_quant.deployment --deployment /data/product/deployment.json backup /backup/product-with-sources --include-source-assets
```

这是显式的大容量操作。它增加清单中未受保护的源资产，要求 LocalDaily 持久 `writer.lock` 可锁定，使用 SQLite backup 合并私有副本里的已提交 WAL，并完整保存 raw/evidence 等库外文件。前后清单和全量文件 hash 必须稳定，否则拒绝。输出的 `omitted_assets` 精确列出未包含的受保护外部环境、临时资产和原因；不把这些目录称为已备份。此实现验收只用隔离的小型测试源，不复制现有大库。

## 在空目录恢复

先用备份配套的原版本运行环境。正式交付的 runtime 资产含 AD/AQ wheel、hash 锁定依赖清单和 `rebuild_runtime.py`：

```sh
python /delivery/runtime/rebuild_runtime.py /restore/venv --bundle /delivery/runtime --offline
/restore/venv/bin/python -m ashare_quant.deployment restore /backup/product-state /restore/product --expected-sha256 RETAINED_MANIFEST_SHA256
/restore/venv/bin/python -m ashare_quant.deployment --deployment /restore/product/deployment.json doctor
/restore/venv/bin/python -m ashare_quant.deployment --deployment /restore/product/deployment.json task-worker --once
```

离线重建需事先保留匹配平台的 Python 和完整依赖缓存；交付的 wheel/锁文件不等于第三方依赖 wheelhouse。需要联网获取依赖时使用相同 hash 锁定清单去掉 `--offline`。恢复包也保存 `runtime/python` 下的确切 AQ/AD 源码与包元数据、`runtime/requirements.lock` 和恢复说明，可用来核对运行身份。恢复拒绝不同 Python、平台、依赖或源码，不悄悄迁移旧任务。

目标须为空目录。恢复先核对完整清单、文件 hash 和运行身份，再复制，并生成新的 `deployment.json` 与 `restore-receipt.json`。只改运行清单里的存储位置，原始清单保存在 `original-deployment.json`；任务输入、原始出处、native checkpoint 和回执不重写。状态包的正式主库路径仍是外部引用，缺失时 `doctor` 明确报告；已冻结任务仍可用恢复的状态运行。含源资产的包则把主库定位到恢复目录。

用恢复后的配置启动 API/worker，核对原任务结果、资金、持仓和成交，再处理待执行任务。`verify` 和 `restore` 拒绝多余文件、缺失文件、路径越界、内容改变和非空目标；manifest 摘要须来自可信保存的回执。备份工具不会清理原目录，也不会修改旧服务。

## 版本化统一入口模板

[local-entry 模板](../examples/local-entry/README.md) 保存 `data`、`entry.py` 与可迁移的 deployment/catalog 示例，使用正常安装的配对 AD/AQ 包。真实数据、审计清单、质量覆盖文件和账户仍在仓库外。已有入口只增量合入分钟注册字段与对应 HF 条目，不整份覆盖现有目录登记、清理回执或运行配置；代码发布不自动部署或替换正在运行的冻结环境。
