"""Admission facade: references A数达 facts, never acquires or repairs market data."""

from importlib import metadata

from . import __version__
from .contracts import Capabilities, DataBatch, DataQuery, DomainError

ASHARE_VERSION = "0.6.0.dev3"
ASHARE_COMMIT = "281908d655762fabbaa3e8b27d054a3770f95c97"
ASHARE_TREE = "71a44f284de83630b404e3976659b0282f95fdc1"
ASHARE_WHEEL_SHA256 = "617febec1745ec2ca53cd4bdffef6ac5c593020f8ee1fdb6eedc2e0229eb4421"
RQALPHA_COMMIT = "0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"


def capabilities() -> Capabilities:
    try:
        native_installed = metadata.version("rqalpha") == "6.4.1"
    except metadata.PackageNotFoundError:
        native_installed = False
    return Capabilities(
        backend_version=__version__, rqalpha_commit=RQALPHA_COMMIT,
        executable_modes=("fixture", "synthetic") if native_installed else ("fixture",),
        rqalpha_validation=("optional_native_installed_use_scoped_m2_catalogue" if native_installed
                           else "optional_dependency_missing"),
        ashare_data_version=ASHARE_VERSION, ashare_data_commit=ASHARE_COMMIT,
        ashare_data_tree=ASHARE_TREE,
        limitations=(
            "Portable M2 jobs use installed source and explicit local owner bindings through /api/v1/m2/jobs/catalogue. Market data is not distributed. Fixture mode remains synthetic.",
            "Legacy D0/BR1 owner fields below retain the dev3 pin. This candidate installs owner 0.8.0.dev2 from explicitly staged fixed candidate source; use the preserved stable service for D0/BR1 reads.",
            "Native execution requires the fixed native extra and license eligibility. Catalogue and task receipts identify the actually available bounded M2 scope.",
            "The dev3 public listing contract has fixed-scope independent acceptance; backend adapter admission remains pending.",
            "BR1 public snapshot binding and native read interfaces are prepared; no BR1 event loop, fills or recovery experiment is admitted.",
            "Verified PIT, complete corporate actions, verified trade totals and real minute completeness are unavailable.",
            "RQAlpha runs trusted self-owned strategies in local isolated processes; no hostile-code sandbox or commercial permission is assumed.",
            "PostgreSQL and real paper recovery are not validated.",
        ),
    )


class PinnedAshareData:
    """Execution facade remains closed; ResearchReader owns the separate public D0 seam.

    A durable research descriptor is not calendar/status/action/limit execution admission.
    No collection, cache mutation or derived quality decisions here.
    """

    def read(self, query: DataQuery) -> DataBatch:
        raise DomainError(
            "ENGINE_DATA_CAPABILITIES_MISSING",
            f"{query.dataset_id}: Public D0 research descriptors and daily reads exist in A数达 "
            f"{ASHARE_VERSION}@{ASHARE_COMMIT}; execution remains unadmitted. "
            "Use the read-only D0 endpoint or explicit BR1 snapshot binding; neither authorizes a run.",
            422,
        )
