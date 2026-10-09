"""One temporary synthetic fixture run; no native engine or market data request."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="aq-public-fixture-") as tmp:
        result = subprocess.run(
            [sys.executable, "-m", "ashare_quant.cli", "--db",
             str(Path(tmp) / "control.sqlite3"), "demo"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        value = json.loads(result.stdout)
        state, manifest = value["state"], value["manifest"]
        assert state["run"]["status"] == "succeeded"
        assert state["is_test_double"] is True
        assert state["account"]["cash"] == "9000.00"
        assert len(state["fills"]) == 1
        assert manifest["real_strategy_executed"] is False
        print(json.dumps({"result": "PASS", "scope": "synthetic_fixture_only",
                          "is_test_double": True, "real_strategy_executed": False,
                          "cash": "9000.00", "fills": 1,
                          "version": manifest["backend_version"]}))


if __name__ == "__main__":
    main()
