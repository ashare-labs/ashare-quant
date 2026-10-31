# 账户历史与运行时迁移

本篇说明 `/api/v1/paper` 只读历史会话接口，以及独立维护的已结算 v3 模拟账户运行时升级授权与持仓续接流程。

## 只读模拟账户历史（`/api/v1/paper`）

产品级 Paper 服务仅提供已封存模拟会话的只读查看（契约见 [`contracts/paper.openapi.json`](contracts/paper.openapi.json)）：
- **HTTP 路由**：`GET /api/v1/paper/capabilities`（返回 `read_only=true`）、`/sessions`、`/sessions/{id}`、`/sessions/{id}/view`、`/sessions/{id}/reports/{day}/{phase}`。
- **CLI 入口**：`python -m ashare_quant.paper {sessions,status,view,report,serve}`。
- 读取报告时按原会话身份与原生检查点摘要校验完整性，不重放策略或修改账本。

---

## 已结算 v3 账本运行时升级授权

对于由 `OnlineService` 管理的 `declared-inventory-target-minute-partial-cancel.v3` 独立账户，当运行环境升级（如 A数达包或源码更新）后，需通过 `settled_migration_cli` 在已结算边界显式写入运行时升级授权：

```sh
# 1. 只读预检账户状态、原生检查点与新旧运行时指纹
python -m ashare_quant.paper.settled_migration_cli \
  --root "$ACCOUNT_ROOT" \
  --state-sha "$STATE_SHA" \
  --from-sha "$OLD_RUNTIME_SHA" \
  --to-sha "$NEW_RUNTIME_SHA"

# 2. 复核通过后写入 paper-settled-v3-runtime-authorization.v1 授权
python -m ashare_quant.paper.settled_migration_cli \
  --root "$ACCOUNT_ROOT" \
  --state-sha "$STATE_SHA" \
  --from-sha "$OLD_RUNTIME_SHA" \
  --to-sha "$NEW_RUNTIME_SHA" \
  --apply --review-sha "$REVIEW_SHA" --reason "$REASON"
```

- **准入条件**：目标账户的所有历史交易日必须已完成开盘与收盘结算，无未结订单或冻结现金，且与原生检查点逐项一致。
- **指纹核对**：`settled_migration.py` 要求 Python 解释器、RQAlpha 源码及除 A数达外的原生依赖保持一致，并核对当前安装的 A数达文件清单、读取器摘要与已验证的版本转换集合（`DATA_TRANSITIONS`）或同版本显式源码升级。
- **不可变归档**：原始 `state.json` 按摘要备份至 `runtime-origins/`，原生检查点文件逐字节不变。

---

## v3 实际持仓续接（`successor_cli`）

已完成运行时授权的 v3 账户如需进入新的持仓交易日，通过 `python -m ashare_quant.paper.successor_cli` 生成并绑定 `paper-declared-inventory-hold-successor.v1` 续接计划：

- 新交易日的初始持仓目标直接取自前一交易日收盘原生账户的实际持仓数量，不重新注入初始申报库存，也不回补前一日因部分成交留下的目标差额。
- 续接交易日必须属于 A数达日历及已复核的公司行动窗口内，并具备当日盘前/开盘/收盘观察证据。
