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
    daily_jobs, fixed_release, tmp_path, monkeypatch, frequency
):
    _, _, body = daily_jobs
    release, _ = fixed_release
    body = {**body, "frequency": frequency}
    reads = []

    def unexpected_reference(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("missing listing must fail before corporate reference reads")

    monkeypatch.setattr(ReleasedReferences, "query", unexpected_reference)
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path / "task")
    try:
        sid, day = "600000.XSHG", data.days[0]
        # Missing listing evidence does not erase observed raw prices.
        response = data.owner.prices([sid], data.previous[day], day)
        assert len(response["rows"]) == 2
        with pytest.raises(DomainError) as error:
            data.component(sid, day)
        assert error.value.code == "PYTHON_LISTING_GAP"
        assert error.value.details == {"security": sid}
        assert not reads
        assert not data.components and not data.corporate
    finally:
        data.close()
