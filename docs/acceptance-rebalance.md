# Isolated real-calendar rebalance acceptance

`paper-intraday-plan.v3` is a separately named acceptance scenario, with model
`declared-inventory-target-minute-partial-cancel.v3`. It does not mutate a
previously frozen equal-weight strategy, or claim that declared initial stock
inventory resulted from historical trades. The old v1/v2 plan serialization,
empty genesis and holding-successor behavior remain unchanged.

The plan binds the exact three securities, previous observed trading day,
initial quantities, explicit target quantities, daily capture and owner/action
review hashes. The native stock account initializes the declared inventory at
that prior close, plus CNY 30,000 cash. It owns all subsequent cash, positions,
T+1 quantities, reservations, commission and tax. The declaration is included
in each native report; there is no second bookkeeping implementation.

Intents require fresh same-day admission within one 15-minute window. Execution
requires a complete common minute whose **start is later than the received
intent**, followed by the existing 60-second observation delay and raw-response
confirmation. Sell deltas execute before buys. The modeled price is minute
close minus CNY .01 for sells, plus .01 for buys, and unchanged for no delta.
Native limit-price, T+1, cash and portfolio risk remain effective. Capacity is
1% of observed minute volume, rounded to whole lots; unfilled orders cancel.
Unfilled sells cannot fund buys. No exchange liquidity or actual execution is
claimed.

New v3 sessions explicitly model commission at .0003, minimum CNY 5 per order,
and seller stamp tax .0005. The latter reflects the
[SSE notice effective 2023-08-28](https://english.sse.com.cn/news/newsrelease/c/5725967.shtml).
Legacy sessions retain their pinned .001 tax assumption. There is no separately
modeled transfer fee; these are declared simulation costs, not a broker bill.
Cost audit uses actual native fills and the sealed session rates, with numerical
tolerance 1e-8 CNY. New purchases remain unsellable on their purchase day,
including the close receipt; next-session unlocking is separate.

Closing v3 requires AD `ad-current-daily-observation.v2` and the
`sse-ended-and-repeat-daily-agrees.v1` proof. After 15:00 Shanghai, two fresh full
daily responses must agree and the same-day SSE snapshot must show E (closed)
and matching open, close and previous close. The AQ receipt pins the owner
capture and proof hashes. Missing, stale or revised data is rejected. Neither
17:45 nor the old general-client 15:05 software cutoff is imposed on this path.
The observations remain provisional, with unknown guaranteed publication time.

`scripts/run_rebalance_foreground.py` consumes a locally frozen package and
runs one bounded command: `preflight`, `premarket`, `admission`, `opening`,
`closing`, `audit`, `status` or `resume --reason ...`. It provides no daemon,
scheduler, clock override, broker API or automatic window extension. A
previously sealed `--capture` can be supplied for delayed close processing.
After an interrupted worker, explicitly resume and repeat the same phase.
Completed checkpoints can be published after the execution deadline without
new orders; an incomplete execution cannot be restarted beyond that deadline.

The operator must preserve real and controlled evidence separately. Unit tests
substitute transport and clocks, including a Friday-to-Monday calendar, sells,
new holdings, fees, T+1 refusal, limit-price rejection, insufficient actual cash,
duplicate delivery and submit/trade/settlement crash recovery. Only observed
market-day packets plus committed reports count toward live acceptance. A
partial fill is reported as partial coverage rather than manufactured success.
