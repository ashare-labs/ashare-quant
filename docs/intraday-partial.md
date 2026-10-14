# Intraday minute capacity and event replay

The original `prior-close-intent-next-complete-minute.v1` rejects a whole execution packet when any target exceeds 1% of its observed minute volume. That policy remains available for reproducing its historical rejection evidence.

`prior-close-intent-minute-partial-cancel.v2` instead treats participation as a declared matching assumption. Its plan has schema `paper-intraday-plan.v2`, a configurable positive `max_minute_participation` at most 1, and explicit `remainder_policy: cancel_after_minute`. The default participation is still 0.01. This is not an exchange restriction or a promise of available liquidity.

The original strategy target and native order quantity remain unchanged. Capacity is `floor(volume * participation / round_lot) * round_lot`, less quantity already matched in this event. The native RQAlpha matcher publishes ordinary native partial trades and cancels the remaining order. A quantity below one round lot cancels without a trade. Other securities continue matching. There are no fabricated trades or reconstructed balances; native fees, cash constraints, T+1 and atomic checkpoints are retained.

The partial capacity changes do not relax causal information gates: the complete minute must start after the frozen intent; the first source request must begin at least 60 seconds after the minute label; both receipts must agree; current trading state, owner evidence, cash and position controls still apply. The receipt states the capacity model and exposes cancelled order quantities and target gaps.

The foreground v2 runner separates observing and executing. `sample` collects a later actual minute and records its minute event identity without placing an order. `execute` reuses an already sealed event idempotently. Before a successful seal, a rejected candidate is retained as evidence but does not pin the next attempt to that capture. An interrupted local capture index is recovered from the atomic account state. A different capture cannot replace a committed event. Because remainders are explicitly cancelled, later samples do not silently reopen them. A completed replay is allowed after the execution window, while new captures and uncompleted execution remain bounded by the frozen window.

The implementation uses a new plan and independent account. Existing v1 accounts, captures and rejected targets must not be migrated or rewritten.
