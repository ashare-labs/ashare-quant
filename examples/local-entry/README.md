# Local unified entry template

This directory versions the local `data` launcher and its `entry.py`, plus portable
configuration examples. It uses installed, paired AD/AQ packages; it does not
load a Codex task folder or inject a source tree into `PYTHONPATH`.

For a new empty entry directory, copy `data` and `entry.py`, prepare the paired
runtime as `.venv`, and adapt `deployment.example.json` / `catalog.example.json`
to `deployment.json` / `catalog.json`. Replace every `REPLACE_WITH_REVIEWED_*`
marker with an independently retained SHA256 before querying. Refer to existing
raw data through `data_root` and relative asset paths; keep the audited manifest
and quality overlay outside the repository. This example contains no market
observations, audit artifacts, account state, or working credentials.

For an existing entry, merge only the new minute registry/configuration fields
and the matching HF catalogue item. Do not replace its whole catalogue or
manifest: other dataset registrations, cleanup receipts, daily references,
runtime/state paths and frozen environments must survive. Do not initialize,
restart services or replace an active/frozen runtime as a side effect of copying
these templates.

```sh
./data minute-sources
./data minute --source neigezhu --security 600000.XSHG \
  --start 2026-06-03T09:40:00+08:00 --end 2026-06-03T09:42:00+08:00
./data minute --security 600000.XSHG --day 2026-10-09
```

Default minutes remain TDX. HF is explicit, native labels and quality decisions
remain visible, and unavailable sources never fall back. The generic financial,
factor, daily and service commands in `entry.py` require their own existing
catalogue entries; the minimal examples intentionally do not invent those
assets or their evidence pins. `list`/`show` display the catalogue metadata;
`minute-sources` resolves the authoritative query bindings from deployment.

Use the [runtime guide](../../docs/data-runtime.md) for paired wheels, explicit
initialization and service commands, and the [minute API guide](../../docs/product-minute.md)
for request fields and configuration-change handling. The pre/post deployment
checks are not an atomic filesystem snapshot: keep fixed source paths while a
service runs, then restart after an authorized migration.
