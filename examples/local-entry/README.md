# Local unified entry template

This directory versions the local `data` launcher and its `entry.py`, plus portable
configuration examples. It uses installed, paired AD/AQ packages; it does not
load a Codex task folder or inject a source tree into `PYTHONPATH`.

For a new empty entry directory, copy `data`, `entry.py` and `account_entry.py`, prepare the paired
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

## Registered settled accounts

The optional `account_entry.py` adapter reads explicitly migrated OnlineService
accounts from `state/online/registry.json`. It is a deployment adapter outside
the execution package; adding it does not change the engine pin or the four
frozen AQ HTTP contracts. It does not import directory ledgers into SQL paper
sessions and exposes no account mutation or execution endpoint.

The registry schema is `ashare-online-account-registry.v1`, with an `accounts`
array. Each entry declares `account_id`, `label`, `path` (equal to account_id),
`identity`, `head`, `runtime_pin_sha256`, `review_sha256`, and `files`. Each file
entry maps its relative name to `bytes` and `sha256`. Register the complete
post-migration account, including the original state archive, but exclude lock
files. Preserve the original source and review material independently. This
template never creates or migrates an account as a side effect.

The reader checks the complete file inventory, rejects links or path escapes,
checks the installed runtime pin, performs strict OnlineService loading, and
validates the native closing report. Changed files or runtime require explicit
review and re-registration. Add `account_registry` metadata to the local
catalogue to include account validation in `data doctor`; installations without
registered accounts keep their ordinary data readiness checks.

```sh
./data accounts
./data account ACCOUNT_ID
./data api
./data web
```

The local API command mounts read-only `GET /api/v1/online-accounts` and
`GET /api/v1/online-accounts/{account_id}`, returning
`local-online-accounts.v1`. The quant-web `#/accounts` page shows the ledger's
cash, NAV, holdings, orders, trades and migration identity. The ordinary
`ashare_quant.deployment api` command does not mount this local extension.
Missing registrations produce an explicit unavailable response, never a sample
or empty replacement account. Requests do not authorize the next trading day.

Retain the entire `state/online` directory, registry and migration evidence when
preparing recovery media. The existing SQL-only paper backup path does not
automatically include these directory accounts. A runtime rebuild must use the
registered paired wheel and dependency pins before strict account recovery.
