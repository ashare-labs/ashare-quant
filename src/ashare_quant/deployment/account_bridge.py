"""Read settled accounts using their separately pinned retained runtime.

The market/API process never imports the old execution engine or submits orders.
Only the two read-only entry commands are available through this bridge.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from ashare_quant.contracts import DomainError


class AccountReader:
    def __init__(self, root, specification):
        self.root = Path(root).resolve()
        self.specification = specification

    def read(self, account_id=None):
        spec = self.specification
        for member in spec["files"]:
            path = (self.root / member["path"]).resolve()
            if (
                not path.is_relative_to(self.root)
                or hashlib.sha256(path.read_bytes()).hexdigest() != member["sha256"]
            ):
                raise DomainError(
                    "ACCOUNT_READER_CHANGED", "Retained account reader differs from its pin", 503
                )
        if account_id is not None and not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", account_id
        ):
            raise DomainError("ACCOUNT_ID_INVALID", "Invalid registered account identifier", 400)
        python = (self.root / spec["python"]).absolute()
        entry = (self.root / spec["entry"]).resolve()
        if (
            ".." in Path(spec["python"]).parts
            or not python.is_relative_to(self.root)
            or not entry.is_relative_to(self.root)
        ):
            raise DomainError(
                "ACCOUNT_READER_PATH", "Retained runtime must belong to this deployment", 503
            )
        args = [str(python), "-I", "-B", str(entry)]
        args.extend(["accounts"] if account_id is None else ["account", account_id])
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
            value = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "ACCOUNT_READER_UNAVAILABLE", "Retained account reader failed", 503
            ) from exc
        if result.returncode or "error" in value:
            raise DomainError(
                "ACCOUNT_READER_REFUSED", "Retained runtime refused account verification", 409
            )
        return value


def mount(app, reader):
    from fastapi.responses import JSONResponse

    def respond(account_id=None):
        try:
            return JSONResponse(reader.read(account_id), headers={"Cache-Control": "no-store"})
        except DomainError as exc:
            return JSONResponse(
                {"error": exc.body()}, status_code=exc.status, headers={"Cache-Control": "no-store"}
            )

    @app.get("/api/v1/online-accounts", tags=["registered online accounts"])
    def list_accounts():
        return respond()

    @app.get("/api/v1/online-accounts/{account_id}", tags=["registered online accounts"])
    def account(account_id: str):
        return respond(account_id)
