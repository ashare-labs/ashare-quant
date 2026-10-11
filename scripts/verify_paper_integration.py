"""Offline research replay and local UI examples; never online execution admission."""

import argparse
import json
import runpy
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.paper.replay import advance, bind
from ashare_quant.paper.service import PaperService

REPO = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--owner-input-dir", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    manifest_path = REPO / "examples/daily_portfolio/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    service = PaperService(out / "store")
    results = {
        "schema": "paper-integration-evidence.v1",
        "continuous_paper_accepted": False,
        "synthetic": [],
        "historical": [],
    }
    synthetic_manifest = json.loads(manifest_path.read_text())
    synthetic_manifest["execution"]["initial_cash"] = "10000"
    generate = runpy.run_path(str(REPO / "scripts/verify_daily_portfolio.py"))["generate"]
    doc = generate(synthetic_manifest, constant=True)
    for row in doc["daily"]:
        row["suspended"] = False
    m, p = out / "synthetic-manifest.json", out / "synthetic-input.json"
    write(m, synthetic_manifest)
    write(p, doc)
    # Only this explicit synthetic 10,000 CNY fixture uses the wider gross/cash
    # policy. Historical sessions below all retain foundation default risk.
    fixture_risk = {"max_gross": "1", "cash_buffer": "0"}
    for strategy in ("equal-weight", "momentum", "trend"):
        sid = bind(service, m, p, strategy, "w2020", "ui-synthetic-" + strategy, risk=fixture_risk)[
            "session_id"
        ]
        head, reports = None, []
        for day in ("2020-02-03", "2020-02-04", "2020-02-05"):
            report = advance(service, sid, day, head)
            assert advance(service, sid, day, head) == report
            reports.append(
                {
                    "day": day,
                    "head": report["checkpoint_sha256"],
                    "trades": len(report["native"]["trades"]),
                    "account": report["native"]["account"],
                }
            )
            head = report["checkpoint_sha256"]
        results["synthetic"].append({"session_id": sid, "strategy": strategy, "reports": reports})
    # A retained interrupted day gives the human UI a concrete recovery action.
    sid = bind(service, m, p, "equal-weight", "w2020", "ui-recovery", risk=fixture_risk)[
        "session_id"
    ]
    try:
        advance(service, sid, "2020-02-03", None, fault="inside_native_trade")
    except DomainError as exc:
        assert exc.code == "PAPER_NATIVE_INTERRUPTED"
    results["recovery_session"] = sid
    sid = bind(service, m, p, "equal-weight", "w2020", "ui-risk-rejected")["session_id"]
    try:
        advance(service, sid, "2020-02-03", None)
    except DomainError as exc:
        assert exc.code == "PAPER_GROSS_LIMIT"
    assert service.status(sid)["session"]["head"] is None
    results["risk_session"] = sid
    if args.owner_input_dir:
        for window in manifest["windows"]:
            for strategy in ("equal-weight", "momentum", "trend"):
                source = args.owner_input_dir / (window["id"] + "-model.json")
                sid = bind(
                    service,
                    manifest_path,
                    source,
                    strategy,
                    window["id"],
                    f"historical-{window['id']}-{strategy}",
                    accept_model=True,
                )["session_id"]
                view = service.view(sid)
                item = {
                    "session_id": sid,
                    "strategy": strategy,
                    "window": window["id"],
                    "input_sha256": view["replay"]["binding"]["input_sha256"],
                    "risk": view["config"]["risk"],
                    "committed_days": [],
                    "stopped": None,
                }
                head = None
                for day in view["replay"]["days"]:
                    try:
                        report = advance(service, sid, day, head)
                        assert advance(service, sid, day, head) == report
                        head = report["checkpoint_sha256"]
                        item["committed_days"].append(
                            {
                                "day": day,
                                "head": head,
                                "cursor": report["native"]["cursor"],
                                "settlement_count": report["native"]["settlement_count"],
                                "trade_count": len(report["native"]["trades"]),
                                "account": report["native"]["account"],
                            }
                        )
                    except DomainError as exc:
                        assert service.status(sid)["session"]["head"] == head
                        item["stopped"] = {
                            "day": day,
                            "code": exc.code,
                            "message": str(exc),
                            "head_unchanged": True,
                        }
                        break
                results["historical"].append(item)
                write(out / "verification.json", results)
    write(out / "verification.json", results)
    print(
        json.dumps(
            {
                "output": str(out),
                "synthetic": len(results["synthetic"]),
                "historical": [
                    {
                        "strategy": x["strategy"],
                        "window": x["window"],
                        "days": len(x["committed_days"]),
                        "stopped": x["stopped"],
                    }
                    for x in results["historical"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
