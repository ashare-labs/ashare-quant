"""The frontend snapshots must equal the actual local HTTP schema responses."""

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.paper.api import create_app as create_paper_app


def test_generated_documents_equal_runtime_endpoints(tmp_path):
    output = tmp_path / "contracts"
    generator = Path(__file__).resolve().parents[1] / "scripts/generate_contracts.py"
    subprocess.run([sys.executable, str(generator), "--output", str(output)], check=True,
                   capture_output=True, text=True)
    app = create_app(str(tmp_path / "control.sqlite3"), market_stores={}, diagnostics_configuration={})
    with TestClient(app) as client:
        for name, endpoint in (
            ("api", "/api/v1/openapi.json"),
            ("tasks", "/api/v1/tasks/daily/openapi.json"),
            ("portfolio", "/api/v1/portfolio/openapi.json"),
        ):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.json() == json.loads((output / f"{name}.openapi.json").read_text())
    with TestClient(create_paper_app(tmp_path / "paper")) as client:
        response = client.get("/api/v1/paper/schema")
        assert response.status_code == 200
        assert response.json() == json.loads((output / "paper.openapi.json").read_text())
