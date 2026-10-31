# 发布绑定的财务质量查询

产品 API 暴露 `POST /api/v1/product/financial/point` 与 `GET /api/v1/product/financial/coverage`。两个端点均使用所选公共市场发布、已安装的 AD 内容绑定、配置快照及既有的同源保护机制，不会提交异步任务或修改账户状态。

单点查询请求包含 `security`、`field`、`period`、`source`、带时区的 `as_of` 以及 `mode`。默认模式为 `official_daily`，可显式切换为 `historical_pit` 或 `source_research`。查询原生报表字段时需同时提供 `statement` 与 `title`。单位核验证据本身不授予历史可得性许可。响应包含发布回执、精确字段证据以及字段值或不可用原因。

覆盖查询接受可选的 `security` 与 `day` 参数。冻结的修正台账明确区分已退役交易代码、经审核的同证券跨源别名、显式停牌日、退市及未解决的缺失记录；停牌绝不会被转换为零价格或无限可交易声明。

[pyproject.toml](../pyproject.toml) 中的版本声明与 [AD 运行时 pin](../src/ashare_quant/providers/data-runtime.json) 共同提供上述读取能力；账户桥接保留其独立运行时。运行时重建输入与回退配置保存在本地部署目录中，无需额外部署常驻服务。
