# Conditional calendar intent (candidate v2)

The new opt-in model is `prior-close-conditional-intent-observed-open.v2`.
The v1 model, historical simulation behavior and activated 14–15 observation
remain unchanged. This patch neither changes an actual identity nor starts a
collector, native worker, service or scheduler.

The previous contract required a current-day normal quote before 09:00. In the
real 14th preflight Sina still returned the 13th; preparation therefore never
ran. The new model freezes the prior-close strategy targets at 09:00–09:05
using AD v5's explicit preparation result. It preserves unknown live trading
state as `suspended=None`. Preparing an intent creates no orders, fills or native
account checkpoint.

Before any native execution, a separate AD v5 opening capture must refresh all
114 sources and prove current normal trading. AQ independently checks the
opening window, receipt ordering, security set, eligible/non-ST/non-suspended
state, unchanged reference/limits/rule, and the same action-review baseline.
The opening capture and action proof hashes are sealed with the immutable input.
The native data source uses this gated execution state; the original decision
retains its unknown state. Cash, order-size, concentration and T+1 checks still
run in the native ledger. Reconnect, restart and duplicate delivery reuse the
sealed input and checkpoint identities.

AD v5 and AQ v2 must be selected together. Missing v5 execution proof, v4 fallback,
or v5 input into the v1 model is refused. The default owner collector remains v4.
An explicit empty-account migration can select v2 only through a hashed reviewed
plan, expected exact prior state and retained byte-for-byte migration archive.
Existing intents/checkpoints still prohibit migration. There is no implicit
model switch and no migration performed by these tests.

The fill reference remains the day opening price for an intent frozen before
the 09:15 auction. Actual events are dated at the opening receipt, and no D
high/low/close/volume is exposed to the opening worker. A new intent created
after today's open cannot use this model to obtain a first real day. An intraday
model would require a new model identity, a later quote observed after that
intent, separate price semantics, native source changes and independent review.
It is not implemented by changing this model's clocks.

For a new 15–16 plan, the parent must first coordinate the distinct AD action
baseline including the 16th, reviewed calendar evidence, matching owner/engine
pins and an updated foreground runner. Its admission selector must require
`schema=v5 && preparation_ready && blockers=[]`; its opening selector must
require `schema=v5 && execution_ready && opening_ready && blockers=[]`. The
collector calls must explicitly pass `--conditional`. The old fixed runner
must remain archived, and the failed 14th must remain failed.

Tests use isolated fixture accounts only: unknown-state preparation without
orders; refusals before native invocation; exact source-parameter comparisons;
two-day native continuity; controller restart; crash inside native trade and
resume; duplicate opening; cash/holdings reconciliation; concentration refusal;
v1 refusal; adapter downgrade refusal; explicit empty model migration. These
tests are supporting evidence, not two actually observed trading days.
