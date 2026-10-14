# Bound holding-day runtime authorization

This is an explicit source-only migration before a bound successor day starts.
The operator must select the exact existing state SHA, original bound runtime
SHA, target runtime SHA and complete continuation digest. The RQ runtime and
installed dependencies must be identical. A started successor, new successor
directory, noncommitted parent, changed checkpoint, changed plan/manifest,
changed state or changed runtime refuses before writing authorization.

`python -m ashare_quant.paper.runtime_migration_cli` defaults to check-only.
`--apply` additionally requires `--review-sha` and a nonblank `--reason` to record
the independent review and explicit operator action. The SHA records evidence;
it does not itself verify the contents of that review or grant approval.

On apply, both the existing global account lock and controller writer lock are
held. `runtime-origin.json` preserves the exact bound control file. The original
`continuation` object moves unchanged to `archived_continuation`; its complete
digest and original binding event remain unchanged. A separately sealed
`runtime_authorization` records old/new runtimes, original state SHA, review SHA,
time, reason and hashes of every original-day file. One control event binds its
complete digest. Identity, configuration, head, completed days, original event
prefix and all native files stay unchanged. There is no account initialization,
cash/position posting or native event during migration.

Removing the old field is deliberate: the frozen c5 controller falls back to its
original-runtime check and rejects execution with `PAPER_RUNTIME_CHANGED`.
Appending an authorization while leaving the old field active would allow that
old controller to ignore the new authorization. Operators must still quiesce old
dispatchers and install the selected new runtime only after apply succeeds.

The new controller validates the archived original state, original binding and
authorization on every load. Native requests include both binding records;
workers verify the selected authorization and record its digest together with
the unchanged continuation digest in native receipts. Later days still have the
original intent/execution windows. Late closing follows `calendar-online.md`.

If interrupted after saving the backup, the original state remains active and
the identical apply can retry. If interrupted after atomic authorization, the
same parameters return the existing authorization without appending another
event, even after the successor starts. Changed retry parameters refuse. Do not
edit pins, delete authorization, restore a stale control file, or reinitialize as
recovery. This implementation does not migrate an already started successor or
support a second runtime replacement.

The real account is not migrated by preparing this candidate. Independently
review its code and targeted evidence, verify the frozen package, then run the
generated exact check/apply commands from the task handoff. Use fresh genuine
review evidence for `--review-sha`; test-copy review hashes are not approvals.
