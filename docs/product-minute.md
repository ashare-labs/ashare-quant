# Registered minute queries through the product API

The normal product API delegates minute reads to the installed, content-pinned
AD `LocalMinuteProduct`. Use the existing deployment/product profile, including
its explicit initialized control state and paired AD runtime. No owner override,
filesystem path, repair option, or execution acceptance flag is accepted in a
request. These endpoints do not create backtests or publications.

`GET /api/v1/product/minute/sources` returns the configured source IDs, default
`tdx`, exact asset/evidence bindings, registered coverage and limitations. Source
discovery does not scan the library. Malformed registrations are reported per
source. Select the returned ID explicitly for an HF read:

```json
{
  "security": "600000.XSHG",
  "start": "2026-06-03T09:40:00+08:00",
  "end": "2026-06-03T09:42:00+08:00",
  "source": "neigezhu"
}
```

Send that body to `POST /api/v1/product/minute/prices`. Omitting `source` selects
TDX; `auto` is not an implicit fallback. Timestamps require offsets and whole
minutes. Bounds are inclusive, one security and the complete requested range, without fixed day or row ceilings.
BJ retains the exact native identifier `BJ.920000` rather than a guessed mapping.
The existing daily `/prices` endpoint and its `auto` source policy are unchanged.

The response preserves AD's `source`, `binding`, `rows`, and `report`. Bad rows,
zero-volume rows, source labels, exceptional numeric encodings, audit evidence,
and row-specific denial decisions are not filtered. No rows or unknown coverage
are promoted to complete trading days. The reader's constant false execution
flag means no reader-issued grant, not a prohibition on all research. Existing
bounded consumers still require their own reviewed evidence and cannot override
a source-bound row denial; see [source use](minute-source-use.md).

Configuration identity includes the minute registry and resolved asset paths.
The API retains its startup configuration. A semantic deployment edit, a changed
resolved asset path, or an unreadable deployment latches
`PRODUCT_CONFIGURATION_CHANGED` (409) and requires restarting the API. Minute
reads check the configuration before opening the owner, against the owner's
loaded document, and after the read; a changed binding cannot be returned under
the old identity. `/config` then reports `configured=false` with the restart
error and the startup identity, rather than claiming the changed environment is
ready. Restoring the file alone does not clear the latched state. On restart,
the new resolved configuration and minute bindings share a new identity; even
a path-only change to identical content changes that identity. JSON whitespace
and key ordering alone do not change the configuration. Content/pin failures
within a fixed configuration remain source-specific and do not trigger this
deployment-change condition.

Existing product middleware
continues to enforce local origin checks and `Cache-Control: no-store`; query
parameters and unrecognized JSON fields are rejected. The API uses the normal
AD runtime and dependency verification, including wheel/source pins.

The current Web product source selector is a daily publication interface. Its
Store alias/dataset browser is also not this native-library registry. A future
small minute query panel can reuse the normal product client/origin handling,
fetch `/minute/sources`, and display `/minute/prices` including row quality. It
must not feed native minute results into daily publication or silently treat
them as execution inputs. This change does not add or deploy a Web interface.

The pre/post checks are not an atomic filesystem snapshot. A transient symlink
switch to byte-identical content and back may retain the old configuration
identity while reporting the temporary resolved path. Different content still
fails its existing pin. Use fixed non-symlink source paths for the reviewed
integration; restart after an authorized asset migration. Supporting runtime
link switching is outside this bounded configuration contract.
