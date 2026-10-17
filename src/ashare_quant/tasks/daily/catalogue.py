"""Project frozen owner documents without private result archives or live data IO."""

import json

from ashare_quant.providers.daily.owner_wire import source_pin
from ashare_quant.tasks.daily.contracts import AllowedWindow, PortableDataIdentity
from ashare_quant.tasks.result_contracts import M2Assumption

OWNER_TEXT = {
    "A-EQ": "限定建模域内假定没有相关权益事件；未证明无事件。",
    "A-NORMAL": "限定窗口的普通股挂牌及交易规则投影。",
    "A-AD08": "仅P日raw close乘限定ratio=1，非完整复权能力。",
    "A-VIS": "事后日线条件研究，不证明历史当时已可见。",
}
BACKEND_TEXT = {
    "A-CLOSE": (
        "matching_policy_sha256",
        "m2.A-CLOSE.current_close.conditional.v1",
        "15:00当日收盘模拟；0滑点，1%成交量上限，原生限价和停牌检查。",
    ),
    "A-COST": (
        "fee_policy_sha256",
        "m2.A-COST.rq641.fixed.v1",
        "固定历史样本：佣金max(5,成交额×0.0008)，卖出税0.001；非现行市场费率声明。",
    ),
    "A-POOL": (
        "pool_policy_sha256",
        "m2.A-POOL.600000.fixed.v1",
        "固定600000普通股、现货多头、T+1、初始零仓。",
    ),
}


def window(binding, window_id):
    entry = binding.entry(window_id)
    data, bound, model = entry.binding, entry.owner_binding, entry.profiles[0]
    desc, plan = json.loads(bound.descriptor), json.loads(bound.window_plan)
    refs = [json.loads(ref) for ref in bound.assumption_refs]
    assumptions = [
        M2Assumption(
            assumption_id=a["assumption_id"],
            owner="ashare-data",
            description=OWNER_TEXT[a["assumption_id"]],
            sha256=a["content_sha256"],
            version=a["version"],
        )
        for a in refs
    ]
    assumptions += [
        M2Assumption(
            assumption_id=key,
            owner="ashare-quant",
            description=description,
            sha256=getattr(model, field),
            version=version,
        )
        for key, (field, version, description) in BACKEND_TEXT.items()
    ]
    return AllowedWindow(
        scope=dict(
            window_id=window_id,
            trading_dates=tuple(d.trading_date for d in entry.windows[0].days),
            initial_cash="10000.00",
        ),
        assumption_ack_sha256=model.profile_sha256,
        assumptions=tuple(assumptions),
        data_identity=PortableDataIdentity(
            owner_version=data.owner_version,
            owner_commit=data.owner_commit,
            owner_runtime_sha256=data.owner_runtime_sha256,
            dataset_id=data.dataset_id,
            manifest_sha256=data.manifest_sha256,
            profile_sha256=data.use_profile_sha256,
            query_contract_sha256=data.query_contract_sha256,
            data_binding_sha256=binding.identity,
            original_quality=json.loads(entry.original_unknowns[0]),
            owner_contract_review=source_pin()["contract_review"],
            owner_descriptor=desc,
            source_references=(plan["source_evidence"],),
        ),
    )
