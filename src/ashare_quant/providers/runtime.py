"""One installed data-owner candidate shared by every AQ consumer."""

import hashlib
import json
import sys
from importlib import metadata
from importlib.resources import files
from pathlib import Path

from ashare_quant.contracts import DomainError


def pin():
    return json.loads(files("ashare_quant.providers").joinpath("data-runtime.json").read_text())


def verify():
    return pin()


def installed_paths(python=None, source=None):
    """Return the data-owner pin without rejecting editable source trees or custom paths."""
    return pin()
