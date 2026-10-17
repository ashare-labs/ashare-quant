"""Execution scope, not a market calendar: owner must substantiate each same link."""

from ashare_quant.storage.canonical import digest, require

CHAIN = (
    "2020-01-02",
    "2020-01-03",
    "2020-01-06",
    "2020-01-07",
    "2020-01-08",
    "2020-01-09",
    "2020-01-10",
    "2020-01-13",
    "2020-01-14",
    "2020-01-15",
    "2020-01-16",
    "2020-01-17",
    "2020-01-20",
)
DAYS = {"two_day": CHAIN[1:3], "w1": CHAIN[1:11], "w2": CHAIN[2:12]}
POLICY = {
    "schema": "m2.portable-scope.v1",
    "security": "600000.XSHG",
    "frequency": "1d",
    "windows": DAYS,
    "required_chain": CHAIN,
    "models": [
        "m2.A-CLOSE.current_close.conditional.v1",
        "m2.A-COST.rq641.fixed.v1",
        "m2.A-POOL.600000.fixed.v1",
    ],
    "visibility": "assumed",
    "initial_positions": "empty",
    "initial_entitlements": "empty",
}
POLICY_SHA = digest(POLICY)


def validate_plan(plan):
    dependencies = plan.get("owner_validation_dependencies")
    require(
        isinstance(dependencies, dict)
        and dependencies.get("consumer_read_permission") is False
        and dependencies.get("security") == POLICY["security"],
        "M2_VALIDATION_DOMAIN_NOT_READABLE",
        "Owner validation dependencies are evidence only, never read authority",
    )
    geometry = plan["geometry"]
    name = geometry["window_id"]
    require(
        name in DAYS and geometry["security"] == POLICY["security"],
        "M2_WINDOW_NOT_REGISTERED",
        "Only the three original single-security windows are allowed",
    )
    links = [
        {
            "current": day,
            "previous_open": CHAIN[CHAIN.index(day) - 1],
            "next_open": CHAIN[CHAIN.index(day) + 1],
        }
        for day in DAYS[name]
    ]
    require(
        geometry
        == {
            "window_id": name,
            "security": POLICY["security"],
            "start": DAYS[name][0],
            "end": DAYS[name][-1],
            "warmup": links[0]["previous_open"],
            "settlement_successor": links[-1]["next_open"],
            "calendar_links": links,
        },
        "M2_WINDOW_NOT_REGISTERED",
        "Owner must substantiate the exact approved P/T/next scope",
    )
    roles = []

    def role(day, phase, kind, target, operations):
        roles.append(
            dict(
                current_date=day, phase=phase, role=kind, target_date=target, operations=operations
            )
        )

    for i, link in enumerate(links):
        t, p, n = link["current"], link["previous_open"], link["next_open"]
        if i == 0:
            role(t, "INITIALIZE", "warmup", p, ["listing"])
            role(t, "INITIALIZE", "current", t, ["listing", "modeled_actions"])
        role(t, "DECIDE", "previous", p, ["decision_prev_close", "listing"])
        role(t, "DECIDE", "current", t, ["listing", "modeled_actions"])
        role(
            t,
            "SUBMIT_MATCH",
            "current",
            t,
            ["execution_bar", "trading_state", "listing", "modeled_actions"],
        )
        for phase in ["AFTER_TRADING", "SETTLEMENT"]:
            role(t, phase, "current", t, ["valuation_bar", "listing", "modeled_actions"])
        role(t, "SETTLEMENT", "settlement_successor", n, ["successor", "listing"])
    require(
        plan["read_roles"] == roles,
        "M2_ROLE_SCOPE",
        "Owner read domains must match the bounded execution policy",
    )
    return name
