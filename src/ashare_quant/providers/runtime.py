"""One installed data-owner candidate shared by every AQ consumer."""

import json
from importlib.resources import files


def pin():
    return json.loads(files("ashare_quant.providers").joinpath("data-runtime.json").read_text())


def verify():
    return pin()


def installed_paths(python=None, source=None):
    """Return the data-owner pin without rejecting editable source trees or custom paths."""
    return pin()
