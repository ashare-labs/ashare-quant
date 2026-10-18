# 数据与诊断

AQ 不抓取、修复或重新认定数据。所有功能使用同一 A数达候选：Store、portable 日频绑定、DailyResearchView、DailyExecutionView、分钟证据、公司行动导出和观察接口。

通用只读入口 `/api/v1/market`；CLI 为 market-describe、market-read、market-coverage。`ASHARE_QUANT_MARKET_STORES` 配置显式别名到 Store 路径的 JSON。金额、整股量、日期、quality 和 lineage 保留原语义；描述符不授予执行权限。

source-diagnostics-capabilities 和 source-diagnostics 使用配置日历 capture 与 Store.preflight_research_request。readable、缺字段、未知和拒绝分别呈现；日历可读不能推断可交易。诊断使用同一候选解释器。

PortableConsumerBinding、PortableReadContext、PortableRead、PortableDocument 在 ashare_data.research。Store.portable、portable_profiles、import_portable 和组件组合保持唯一数据所有者。

pin 记录真实分发版本、源码清单与 wheel 摘要。新源码不得复制历史 PASS receipt。更新 pin 后验证导出消费、失败边界、原生执行及 wheel。

分发版本和 portable owner 文档版本分别固定。当前 AD 分发为 `0.10.0.dev5`，portable owner 文档版本保持 `0.10.0.dev1`。pin 的 `version` 对应实际安装包，`portable_owner_version` 对应 AD 描述符；AQ DataBinding.owner_version 仍记录已安装分发版本。不能用包版本替换既有描述符中的版本、policy 或 parser 身份。
