"""An unavailable execution prerequisite must fail before broad reference I/O."""

import pytest

from ashare_data.product.released_references import ReleasedReferences
from ashare_quant.contracts import DomainError
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("fixed_release", [{"missing_listings": ["600000.XSHG"]}], indirect=True)
def test_missing_listing_refuses_execution_before_corporate_scan(
    daily_jobs, fixed_release, tmp_path, frequency
):
    _, _, body = daily_jobs
    release, _ = fixed_release
    body = {**body, "frequency": frequency}
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path / "task")
    try:
        sid, day = "600000.XSHG", data.days[0]
        response = data.owner.prices([sid], data.previous[day], day)
        assert len(response["rows"]) == 2
        assert data.is_paused(sid, day) is False
        if frequency == "1d":
            assert data.component(sid, day) is not None
    finally:
        data.close()
