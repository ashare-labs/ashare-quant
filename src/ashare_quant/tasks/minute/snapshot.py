"""Immutable bounded release responses adapted to AD's existing minute exporter."""

import hashlib
import json
from bisect import bisect_right
from datetime import datetime, timedelta


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode()
    ).hexdigest()


class ReleasedQueries:
    def __init__(self, release_receipt, bundles):
        self.bundles = bundles
        self.snapshot_id = digest({"release": release_receipt, "bundles": bundles})
        self.release_receipt = release_receipt

    def lineage(self):
        return {
            "snapshot_id": self.snapshot_id,
            "market_release": self.release_receipt,
            "batches": [{"source": b["source"], "sha256": digest(b)} for b in self.bundles],
        }


class IndexedQueries(ReleasedQueries):
    """Same read-only snapshot protocol, indexed to avoid repeated year scans."""

    def __init__(self, receipt, bundles):
        super().__init__(receipt, bundles)
        self.by_symbol = {}
        self.unavailable = {}
        for b in bundles:
            for row in b.get("unavailable", []):
                key = row["security"], row["source_label"]
                assert key not in self.unavailable
                self.unavailable[key] = {
                    **row,
                    "source_id": b["source"]["id"],
                    "source_contract_sha256": digest(b["source"]),
                }
            for row in b["bars"]:
                self.by_symbol.setdefault(row["symbol"], []).append(
                    (row["timestamp"], row, b["source"])
                )
        for rows in self.by_symbol.values():
            rows.sort(key=lambda x: x[0])
        self.labels = {s: [r[0] for r in rows] for s, rows in self.by_symbol.items()}

    @staticmethod
    def project(r, source):
        label = datetime.fromisoformat(r["timestamp"])
        return {
            "symbol": r["symbol"],
            "source_label": r["timestamp"],
            "source_id": source["id"],
            "bar_start": (label - timedelta(minutes=1)).isoformat(),
            "bar_end": label.isoformat(),
            "available_at": None,
            "observed_at": source["observed_at"],
            "quality": r["quality"],
            "source_fields": r,
            **{k: r[k] for k in ("open", "high", "low", "close", "volume", "amount")},
        }

    def bars(self, ids, start, end, *, quality, strict, as_of, adjustment, frequency):
        assert quality == "research" and as_of is None and adjustment is None and frequency == "1m"
        rows = []
        for sid in ids:
            labels = self.labels.get(sid, [])
            for _, row, source in self.by_symbol.get(sid, [])[
                bisect_right(labels, start) : bisect_right(labels, end)
            ]:
                rows.append(self.project(row, source))
        begin, finish = map(datetime.fromisoformat, (start, end))
        expected = {
            (s, (begin + timedelta(minutes=n)).isoformat())
            for s in ids
            for n in range(int((finish - begin).total_seconds() / 60))
        }
        keys = [(r["symbol"], r["bar_start"]) for r in rows]
        present = set(keys)
        missing = sorted(expected - present)
        known = []
        unknown = []
        for sid, bar_start in missing:
            label = (datetime.fromisoformat(bar_start) + timedelta(minutes=1)).isoformat()
            quote = self.unavailable.get((sid, label))
            (known if quote else unknown).append(quote or {"symbol": sid, "bar_start": bar_start})
        # Only AD-confirmed unavailable quotes are admitted by this preparer.
        # A missing source window is not silently expanded into synthetic bars.
        if unknown:
            from ashare_quant.contracts import DomainError

            raise DomainError(
                "PYTHON_MINUTE_GAPS",
                "Minute observations are missing",
                422,
                details={"missing": unknown[:50], "count": len(unknown)},
            )
        coverage = {
            "snapshot_id": self.snapshot_id,
            "unknown_calendar_dates": [],
            "excluded": [],
            "unexpected": [{"symbol": s, "bar_start": d} for s, d in sorted(present - expected)],
            "duplicate_keys": len(keys) - len(present),
            "missing": [{"symbol": s, "bar_start": d} for s, d in missing],
            "unavailable_quotes": known,
        }
        if strict and coverage["missing"]:
            raise ValueError("RELEASE_MINUTE_GAPS:" + str(coverage["missing"][:5]))
        return {
            "rows": rows,
            "coverage": coverage,
            "units": {"price": "CNY", "volume": "shares", "amount": "CNY"},
        }
