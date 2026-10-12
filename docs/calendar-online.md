# Foreground calendar paper engine

`ashare_quant.paper.online` is a separate, local controller for actual calendar
arrivals. The historical `PaperService` contracts still reject its input modes.
There is one RQAlpha ledger; controller JSON contains frozen inputs, hashes,
phase status, and publication pointers. Cash and positions are read-only
projections of the native checkpoint.

The owner-only entry point is `python -m ashare_quant.paper.online_cli`. It
requires explicit AD interpreter/source/store paths and pins the full AD source
inventory separately from AQ/native runtime identity. No installed dependency
is upgraded. `calendar_fixture` APIs exist for development; their evidence and
clock labels cannot enter a `calendar_observed` identity.

## Three distinct arrivals

1. Before D 09:00 Shanghai, AD must have actually received P daily history,
   exchange calendar, and effective D eligibility, ST, suspension, limits and
   complete corporate-action absence coverage. D 09:00–09:05 freezes the existing
   equal-weight strategy, native parent, and targets. First-day sizing uses the
   declared initial capital. Later rebalances use the previous native NAV;
   non-rebalance days retain actual native quantities, including prior refusals.
2. D 09:30–09:35 receives a separate opening packet containing only security,
   day, open, source timestamp, local receipt timestamp and raw hash. The native
   event uses the opening receipt time, never an earlier 09:30 timestamp. The
   declared standing-intent model matches at the observed open; it makes no
   claim about exchange queueing, liquidity, or an executable broker price at
   the later receipt time. No D high/low/close/volume is available in this phase.
3. After D 15:05 and by 18:45, independently observed D daily bars are checked
   against the frozen opening price. AD's collection plan starts at 17:45.
   The native process restores the open checkpoint, cancels unfilled orders,
   values at the close and settles exactly once. Native close events use the
   close receipt time. No early price is matched using late close information.

At the opening boundary, the native process atomically records cursor P+2 and
exits before RQ's end-of-iterator settlement. Closing advances to P+4. Opening
and closing immutable checkpoint copies are separately verified against their
native portfolio, event cursor, settlement count, trades and reconciliation.
The mutable working checkpoint can never replace a published phase report.

## Recovery and operational limits

An inherited writer lock and global account lock fence the controller/native
pair. A native parent-death watchdog exits an orphan. Any failed or interrupted
worker requires explicit `resume` with a reason. A completed native checkpoint
can be published after a controller crash without re-executing trades. An
uncommitted phase can only resume within its real clock window. There is no
retrospective catch-up, reset, or skip-day operation. A missing close keeps the
account pending and blocks the next trading day; the supervisor must resolve
that explicitly. Source, input or native evidence changes reject rather than
silently overwrite. Duplicate identical deliveries reuse their original intent
and native report, including old days.

`status` reports runtime/owner mismatches, failed/running recovery, verified
native account projections, risk refusals, and target gaps. CLI operations and
the task wrapper preserve blocked attempts. No persistent process, HTTP service,
OS registration, broker endpoint or timer is installed.

The pilot uses the original three SSE main-board securities, round lot 100 and
T+1. Costs are explicit model parameters inherited from `SessionConfig`, not a
claim about a real brokerage account's fee schedule. Corporate actions remain
`reject`: this adapter requires verified absence and does not infer zero
entitlement or post share/dividend transformations itself.

## Current admission remains closed

The AD `phase_observation` reader currently produces diagnostics and opening
observations but no execution permission. Its missing effective-date state and
all-action coverage cannot be supplied as caller JSON. The CLI therefore records
`PAPER_CURRENT_ADMISSION_BLOCKED` before creating any real intent. A reviewed AD
owner coverage implementation, followed by an explicit new pin/identity policy,
is still necessary to admit real execution. The staged native engine is
implemented; live orders and actual two-day continuation are not yet accepted.

Validation: `tests/paper/test_online.py` exercises separated arrivals, delayed
valuation, three-day continuation, native/controller SIGKILL, idempotency,
source revision, evidence tamper, window rejection, T+1, and independent cash
rejection retaining earlier native fills. These are labelled fixtures, not real
calendar observations. The existing historical paper suite must also pass.
