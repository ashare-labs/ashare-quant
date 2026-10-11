"""Read-only comparison of exposed current quotes and requested minute histories."""

from .compare import clock, decimal, fields

QUOTE_FIELDS = {
    "day_open": "day_open",
    "high_limit": "upper_limit",
    "low_limit": "lower_limit",
    "last_price": "last",
    "paused": "suspended",
    "is_st": "is_st",
}


def compare_markets(jq_events, aq_events):
    comparisons = []
    for event in jq_events:
        if event["kind"] not in {"current_data", "market_bars"}:
            continue
        candidates = [
            e
            for e in aq_events
            if e["kind"] == "market_observation"
            and e["label"] == event["label"]
            and clock(e["at"]) == clock(event["time"])
        ]
        if len(candidates) != 1:
            raise ValueError("MARKET_OBSERVATION_BINDING_AMBIGUOUS")
        actual = candidates[0]
        left = (
            {"at": clock(event["time"]), "security": event["symbol"]}
            if "symbol" in event
            else {"at": clock(event["time"])}
        )
        right = {"at": clock(actual["at"])}
        if "symbol" in event:
            right["security"] = actual["history"]["security"]
        if event["kind"] == "current_data":
            left["quote"] = {
                k: v if type(v) is bool else decimal(v) for k, v in event["data"].items()
            }
            right["quote"] = {k: actual["quote"][v] for k, v in QUOTE_FIELDS.items()}
        else:
            left["rows"] = [
                {k: clock(v) if k == "index" else decimal(v) for k, v in row.items()}
                for row in event["rows"]
            ]
            right["rows"] = [
                {
                    "index": clock(row["source_label_at"]),
                    **{("money" if k == "amount" else k): v for k, v in row["values"].items()},
                }
                for row in actual["history"]["rows"]
            ]
        comparisons.extend(
            fields(
                left,
                right,
                {
                    "label": event["label"],
                    "jq_event_seq": event["seq"],
                    "aq_event_seq": actual["seq"],
                    "kind": event["kind"],
                },
            )
        )
    diffs = [r for r in comparisons if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}]
    return dict(
        status="MARKET_QUERY_FIELDS_MATCH" if not diffs else "MARKET_QUERY_FIELDS_DIFFER",
        comparisons=comparisons,
        differences=diffs,
        fields=len(comparisons),
        historical_visibility="NOT_CERTIFIED",
        independent_source_crosscheck=False,
        basis="Same fixed source observations transported through AD and AQ",
    )
