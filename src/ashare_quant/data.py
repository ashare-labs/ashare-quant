"""Admission facade: references A数达 facts, never acquires or repairs market data."""

from importlib import metadata

from ashare_quant import __version__
from ashare_quant.contracts import Capabilities, DataBatch, DataQuery, DomainError
from ashare_quant.providers.runtime import pin

_owner = pin()
ASHARE_VERSION = _owner["version"]
ASHARE_COMMIT = _owner["commit"]
ASHARE_TREE = _owner["tree"]
ASHARE_WHEEL_SHA256 = _owner["wheel_sha256"]
RQALPHA_COMMIT = "0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"


def capabilities() -> Capabilities:
    try:
        native_installed = metadata.version("rqalpha") == "6.4.1"
    except metadata.PackageNotFoundError:
        native_installed = False
    return Capabilities(
        backend_version=__version__, rqalpha_commit=RQALPHA_COMMIT,
        executable_modes=("python_daily", "python_minute", "factors") if native_installed else (),
        rqalpha_validation=("optional_native_installed_use_task_catalogue" if native_installed
                           else "optional_dependency_missing"),
        ashare_data_version=ASHARE_VERSION, ashare_data_commit=ASHARE_COMMIT,
        ashare_data_tree=ASHARE_TREE,
        limitations=(
            "Custom Python admission is described by /api/v1/python/catalogue; factors use /api/v1/factors/catalogue.",
            "One installed data-owner candidate is shared by market reads, diagnostics, tasks and paper.",
            "New candidate source and execution do not inherit historical independent review.",
            "Unknown PIT, price limits, corporate actions and source clocks remain unknown.",
            "RQAlpha runs trusted local strategies; no hostile-code sandbox, broker access or commercial permission is implied.",
        ),
    )


class PinnedAshareData:
    """Generic descriptions never grant engine admission; use a bound task input."""

    def read(self, query: DataQuery) -> DataBatch:
        raise DomainError("ENGINE_DATA_CAPABILITIES_MISSING",
                          f"{query.dataset_id}: use a task's explicit binding and preflight", 422)
