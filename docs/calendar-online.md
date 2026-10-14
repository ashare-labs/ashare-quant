# Foreground calendar paper engine

`ashare_quant.paper.online` is a separate, local controller for actual calendar
arrivals. The historical `PaperService` contracts still reject its input modes.
There is one RQAlpha ledger; controller JSON contains frozen inputs, hashes,
phase status, and publication pointers. Cash and positions are read-only
projections of the native checkpoint.

The owner-only entry point is `python -m ashare_quant.paper.online_cli`. It
requires explicit AD interpreter/source/store paths and pins the full AD source
inventory and the bundled issuer-review evidence separately from AQ/native runtime identity. No installed dependency
is upgraded. `calendar_fixture` APIs exist for development; their evidence and
clock labels cannot enter a `calendar_observed` identity.

## Three distinct arrivals

1. Before D 09:00 Shanghai, AD must have actually received P daily history,
   exchange calendar, and effective D eligibility, ST, suspension, limits and
   a verified, bounded corporate-action review with actual entitlement checks. D 09:00–09:05 freezes the existing
   equal-weight strategy, native parent, and targets. First-day sizing uses the
   declared initial capital. Later rebalances use the previous native NAV;
   non-rebalance days retain actual native quantities, including prior refusals.
2. D 09:30–09:35 receives a separate opening packet containing only security,
   day, open, source timestamp, local receipt timestamp and raw hash. The native
   event uses the opening receipt time, never an earlier 09:30 timestamp. The
   declared standing-intent model matches at the observed open; it makes no
   claim about exchange queueing, liquidity, or an executable broker price at
   the later receipt time. No D high/low/close/volume is available in this phase.
3. After D 15:05, independently observed D daily bars are checked
   against the frozen opening price. AD's collection plan starts at 17:45.
   The native process restores the open checkpoint, cancels unfilled orders,
   values at the close and settles exactly once. Native close events use the
   close receipt time. No early price is matched using late close information.
   There is no 18:45 settlement cutoff: that was a pilot observation constraint,
   not a source publication guarantee or an execution deadline. The owner must
   still collect on D (its collection plan starts at 17:45). Both late same-day
   collection and later processing of an already verified D capture are allowed.
   New packets omit `deadline`; a legacy sealed packet retains that field and its
   hash, but the calendar path no longer treats it as a settlement deadline.

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
uncommitted opening phase can only resume within its real execution window.
Closing can resume after that day's pilot cutoff, including on a later calendar
date, using the same immutable input and native open checkpoint. Native close
events retain the actual D capture receipt time; controller `worker_started` and
`phase_committed` events retain the actual later processing time. There is no
historical recollection, reset, or skip-day operation. A missing close keeps the
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
`reject` for any required posting/transformation. An owner-reviewed historical
cash event can have zero entitlement when its record date precedes the actual
native genesis and its ex date precedes the decision. That evidence is recorded
explicitly as `zero_entitlement`, never as absent actions.

## Bounded owner review and actual acceptance

AD v3 now binds a sealed three-issuer corpus for October 13–14, 2026 and rereads
all reviewed text pages plus the complete current catalog before each decision.
The baseline is part of the AD code/evidence pin. AQ verifies the per-security
proof hash, security/day/genesis, and continuity with every prior native decision.
Changing a body, adding an unreviewed notice, losing a source or crossing the
reviewed date scope rejects; a zero account balance cannot fill an evidence gap.
Legacy v1/v2 phase receipts do not gain permission from newer code.

The live path can admit only if those action checks and all current market gates
succeed in the real pre-open window. No nightly refresh or fixture result counts
as live admission. In particular, current-day quote and formal-limit timestamps
remain required and must be observed in the morning. Actual two-day native
continuation is still pending. An owner/runtime change requires an explicit
review; `migrate-empty` is only valid before any intent/native account, preserves
the exact old state, and cannot reset an existing account.

Validation: `tests/paper/test_online.py` exercises separated arrivals, delayed
valuation, three-day continuation, native/controller SIGKILL, idempotency,
source revision, evidence tamper, window rejection, T+1, and independent cash
rejection retaining earlier native fills. These are labelled fixtures, not real
calendar observations. The existing historical paper suite must also pass.

The cross-owner fixture uses the original issuer corpus through AD's production
reader, then clearly labels the resulting native run `calendar_fixture`. Run it
with `ASHARE_CALENDAR_AD_SOURCE` pointing at the tested AD worktree's src and
`ASHARE_CALENDAR_AD_PYTHON` pointing at its interpreter; neither variable affects
live identity permissions. It verifies first-day three-security fills, second-day
holdings/cash continuation, T+1 sellability, checkpoint restoration, and duplicate
publication without claiming real-calendar completion.
