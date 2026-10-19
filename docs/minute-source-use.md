# Source use in bounded minute research

`accept_research=True` permits the explicitly declared retrospective profile;
it never overrides a source-bound row denial. It does not certify real fills,
historical availability, all securities or arbitrary held-position lifecycles.
The reader's legacy constant permission booleans mean no reader-issued grant.

AD exports `minute-row-use.v1` with separate `execution` and `fresh_valuation`
decisions: `deny` or `not_granted`. AQ recomputes the decision from the immutable
AD row and checks its source contract/row hashes and native identity/values.
Legacy exports containing a nested native observation are checked too. A
top-level policy cannot weaken a nested invalid/conflict decision.

Explicit invalid/conflict prices deny matching and fresh marking at that exact
label. Hard activity defects deny execution. Conditional amount/VWAP/extrema
diagnostics do not deny prices or create a whole-library lock. They depend on
same-interval/trade-set, exact units and quantity/amount assumptions that a
reference provider can also violate. Mathematical compatibility is not a grant.

The normal default requires a complete declared grid. An exporter may explicitly
select `gap_policy="explicit_unavailable"` for a partial snapshot. It emits no
fabricated OHLC: each unavailable slot has the snapshot coverage report and its
hash. AQ requires the exact union of observed bars and bound unavailable slots;
dropping a bar without its gap evidence still rejects the input. These slots
describe missing quotes in the selected materialization, not proved lost trades,
halts, listing status, or an authoritative historical universe. Unknown calendar
and unresolved source selection cannot be hidden under this policy.

During a denied/missing minute, the matcher refuses the quote before determining
a deal price or capacity. An outstanding order can match at a later usable bar
under the existing order lifetime, price, cash, capacity and T+1 rules. The
native account retains quantity; its previous usable price is a **stale account
mark**, never a fresh quote or a fill price. Position `.valuation` and
`get_current_data()[security].valuation` expose `status`, `source_label`,
`last_unavailable_label`, `price` and `fresh_quote`. Account artifacts retain the
same metadata. A normal subsequent quote restores research marking and matching.

This supports flat-start/flat-end bounded research with explicit no-known-action
scope and ordinary daily states. It does not support arbitrary initial holdings,
corporate-action crediting, delisting liquidation, unknown daily states, or an
unbounded stale-price policy. Store's numeric/OHLC/precision contract is still
strict: malformed numeric rows remain in the native reader and cannot be imported
as valid Store bars. Do not round or replace them; publish only actual valid
coverage and explicitly represent absent quotes when using a partial snapshot.

The 2026-06-02..04 real-source regression is a bounded engineering case. Deliberate
conflict/invalid decisions and a missing quote are injected into separate small
artifacts, never the raw file. They prove denial, stale marking, holding retention
and later recovery; they do not assert those real source quotes were erroneous.
