"""Synthetic transport and baseline initialization for the installed AD reader.

Only child processes serving this test's temporary receipts get synthetic action
pins. The real AQ reader, installed AD code check, receipt validation and native
execution remain active. No product module imports this test helper.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from ashare_data.core.model import DataError, canonical
from ashare_data.observations import action_baseline as action
from ashare_data.observations import intraday as observations
from ashare_data.observations import phases
from ashare_data.observations.daily import sha

from ashare_quant.providers.runtime import verify

DAY = "2026-10-14"
PRICES = {"600000": 10, "600036": 20, "601318": 30}


def activate_baseline(root):
    """Explicit child-only equivalent of AD's synthetic_action_baseline fixture."""
    root = Path(root)
    raw = (root / "manifest.json").read_bytes()
    replan = (root / "replan-20261014-15/manifest.json").read_bytes()
    assert json.loads(raw)["classification"] == "synthetic"
    assert json.loads(replan)["classification"] == "synthetic"
    os.environ[action.EVIDENCE_ENV] = str(root)
    os.environ.pop("ASHARE_ACTION_EXTENSION", None)
    os.environ.pop("ASHARE_ACTION_EXTENSION_SHA256", None)
    action.BASELINE_SHA256 = sha(raw)
    action.REPLAN_SHA256 = sha(replan)


def install_sources(monkeypatch, at, volumes):
    """Supply the same synthetic shapes as AD's clock/transport tests."""
    clock = datetime.fromisoformat(at)
    quote_at = clock - timedelta(seconds=2)
    monkeypatch.setattr(observations, "now", lambda: clock)
    manifest, blobs = action.load_baseline(replan=True)
    issuer = {}
    for code, item in manifest["securities"].items():
        issuer[action.catalog_url(code, DAY)] = blobs[item["catalog_source"]]
        for article, document in item["documents"].items():
            for page, name in enumerate(document["sources"], 1):
                issuer[action.content_url(article, page)] = blobs[name]

    # Load only the sibling test builder, never another repository's test suite.
    import runpy

    dividends = runpy.run_path(str(Path(__file__).with_name("synthetic_actions.py")))["dividends"]

    def fetch(url, referer):
        params = parse_qs(urlparse(url).query)
        if url in issuer:
            return issuer[url]
        if "getKLineData" in url:
            code = params["symbol"][0][2:]
            price = str(PRICES[code])
            return canonical([dict(day=DAY + " 10:02:00", open=price, high=price,
                                   low=price, close=price,
                                   volume=str((volumes or {}).get(code, 1000000)))])
        if "yunhq.sse.com.cn" in url:
            code = url.rsplit("/", 1)[1].split("?")[0]
            price = PRICES[code]
            return canonical(dict(
                date=20261014, time=int(quote_at.strftime("%H%M%S")), total=1, begin=0, end=1,
                list=[[code, "合成证券", price, price, price, price * 1.1, price * 0.9, "T111    "]],
            ))
        if "hq.sinajs.cn" in url:
            lines = []
            for code, price in PRICES.items():
                fields = ["0"] * 33
                fields[:3] = ["合成证券", str(price), str(price)]
                fields[30:] = [DAY, quote_at.strftime("%H:%M:%S"), "00"]
                lines.append('var hq_str_sh' + code + '="' + ",".join(fields) + '";')
            return "\n".join(lines).encode("gb18030")
        if "COMMON_SSE_CP" in url:
            code = params["STOCK_CODE"][0]
            return canonical(dict(result=[dict(A_STOCK_CODE=code, STOCK_TYPE="1", LIST_BOARD="1",
                                               DELIST_DATE="-", LIST_DATE="20000101")],
                                  pageHelp=dict(total=1)))
        if "push2.eastmoney.com" in url:
            code = params["secid"][0][2:]
            price = PRICES[code]
            at = datetime.fromisoformat(DAY + "T08:30:14+08:00")
            return canonical(dict(rc=0, data=dict(f57=code, f86=int(at.timestamp()), f60=price,
                                                  f51=price * 1.1, f52=price * 0.9)))
        if "RPT_SHAREBONUS_DET" in url:
            return canonical(dividends(params["filter"][0].split('"')[1]))
        if "query.sse.com.cn" in url and params.get("sqlId", [None])[0] in {
            "GW_PL_JYTS_TFPXX", "PL_SSGSXX_FXJSBGPLB",
        }:
            return canonical(dict(result=[], pageHelp=dict(total=0)))
        if "RPT_CUSTOM_SUSPEND_DATA_INTERFACE" in url:
            return canonical(dict(success=True, code=0, result=dict(pages=1, count=0, data=[])))
        raise AssertionError(f"Unrecognized synthetic source: {url}")

    monkeypatch.setattr(phases, "fetch", fetch)


def capture(root, phase_store, volumes):
    verify()
    # Prove that the normal installed baseline pin rejects these invented inputs.
    with pytest.raises(DataError, match="审阅基线摘要不符"):
        action.load_baseline(Path(root) / "replan-20261014-15", replan=True)
    activate_baseline(root)
    result = {}
    for kind, at, after in (
        ("admission", "10:00:30", "10:00:00"),
        ("execution", "10:03:30", "10:00:31"),
    ):
        with pytest.MonkeyPatch.context() as monkeypatch:
            install_sources(monkeypatch, DAY + "T" + at + "+08:00", volumes)
            attempt = observations.capture(phase_store, kind, DAY, DAY + "T" + after + "+08:00")
            assert attempt["status"] == "observed", attempt
            packet = observations.read(phase_store, attempt["capture_id"])
            assert packet["assessment"]["ready"], packet["assessment"]["blockers"]
            assert packet["assessment"]["execution_permission"] is False
            result[kind] = attempt["capture_id"]
    return result


def prepare_owner(tmp_path, monkeypatch, volumes):
    from tests.paper.synthetic_actions import write_baselines

    verify()
    source = Path(str(files("ashare_data"))).parent
    root = tmp_path / "synthetic-actions"
    write_baselines(root)
    helper = str(Path(__file__).resolve())
    result = subprocess.run(
        [sys.executable, "-I", "-B", helper, str(root), str(tmp_path / "phases"),
         json.dumps(volumes)],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Execute the production reader's boot code unchanged after the explicit
    # test baseline initialization. All other subprocesses, including RQAlpha,
    # go through untouched; this cannot switch AD code or suppress its pin check.
    run = subprocess.run

    def with_synthetic_baseline(command, *args, **kwargs):
        if (isinstance(command, list) and len(command) > 5
                and command[:4] == [sys.executable, "-I", "-B", "-c"]
                and command[5] == str(source.resolve())
                and any(token in command[4] for token in (
                    "from ashare_data.observations.intraday import read",
                    "from ashare_data.observations.action_baseline import load_baseline",
                ))):
            prefix = (f"import runpy;runpy.run_path({helper!r})"
                      f"['activate_baseline']({str(root)!r});\n")
            command = [*command[:4], prefix + command[4], *command[5:]]
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", with_synthetic_baseline)
    return sys.executable, source, json.loads(result.stdout)


if __name__ == "__main__":
    print(json.dumps(capture(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))))
