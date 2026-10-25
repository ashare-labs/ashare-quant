# 许可与第三方材料

项目权利人尚未为 A宽自有源码指定开源许可证。本次整理保留原有源码预览状态，没有添加 MIT、Apache-2.0 等 LICENSE，也没有授予额外的开源、商业使用或再分发许可。需要这些权限时，应先取得权利人的明确授权；此说明不改变平台条款下已有权利，也不为第三方重新授权。

`ashare-data` 的分发版本、源码提交和文件清单以[运行时 pin](../src/ashare_quant/providers/data-runtime.json)为准。当前固定源码 `38628fe9e17fb96a84fd720f7bc67157d936990d` 的说明位于[来源与许可](https://github.com/ashare-labs/ashare-data/blob/38628fe9e17fb96a84fd720f7bc67157d936990d/docs/sources-and-licensing.md)。源码可取得不替代许可。本仓库不捆绑 AD 二进制，其他依赖适用各自随包许可；软件权利与行情数据权利分别处理。

## 声明来源

2026-10-11 核对时，AQ [已公开的许可说明](https://github.com/ashare-labs/ashare-quant/blob/main/docs/licensing.md)与本地源码预览起点 `0497c7d9d27432ebdbf44e8d26f46db2ebfa14c1` 都明确未指定自有代码开源许可证；本次内部重构未改变这一声明。项目元数据也没有指定 MIT、Apache 或其他自有代码许可证。上面的状态由这些既有材料继承，不是本次新设的发布规则。

本交付可以作为权利人保留既有声明的源码预览候选；不将“没有开源许可”解释成技术安装失败，也不称为已开放第三方自由使用的开源产品。[GitHub 官方说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)区分公开仓库的查看/fork 权利与进一步的软件使用授权。

如果后续希望向第三方开放使用、修改或再分发，需由权利人明确 AQ 与固定 AD 的授权范围。此处没有替权利人选择许可证，也没有将可选 RQAlpha 的许可扩展到自有代码。

## 依赖与数据

可选 `native` extra 为 RQAlpha 6.4.1。保留的[完整许可原文](rqalpha/RQAlpha-LICENSE.txt)来自固定开发候选，未经改写。其条款对非商业个人／非营利教育研究及商业／组织使用作出区分，不能仅凭包元数据中的 Apache-2.0 简写认定所有用途获准。使用者需按完整条款确认资格；本候选在用户授权的个人自用范围运行有限原生研究验证，没有替用户取得商业许可或其他新授权；自有资金投资的官方解释仍未得到确认。

真实有限研究的数据材料来自 BaoStock 原始日线、日历与补充字段，以及交易所规则、公司公告和既有审阅材料。免费查询、免注册或公开可读不等于可以复制整个包、公开分发或商业转售。本候选没有附带该包、供应商原始响应、封存账户、原生检查点、实际研究轨迹或结果数据库。

`tests/fixtures/synthetic/` 中的 JSON 是仅用于开发验证的人为输入，不进入产品 wheel。源码中的证券代码、日期窗口、规则摘要和审阅身份用于描述实现边界，不表示提供了真实行情数据或新增使用授权。后续发行可用研究版本，仍需分别确定自有代码许可、依赖使用资格和数据交付权限。
