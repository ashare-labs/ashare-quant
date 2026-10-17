"""A reviewed producer does not admit this backend, and its receipt cannot float."""

import json

import pytest
from ashare_data import Store

from ashare_quant.contracts import DomainError
from ashare_quant.providers.daily.owner_wire import source_pin
from ashare_quant.providers.daily.portable import bind_dataset
from ashare_quant.tasks.daily.catalogue import window


def test_distribution_and_portable_owner_versions_are_both_preserved(owner_store):
    path, dataset = owner_store
    binding = bind_dataset(Store(path), dataset)
    pin = source_pin()
    for entry in binding.entries:
        descriptor = json.loads(entry.owner_binding.descriptor)
        assert descriptor["version"] == pin["portable_owner_version"]
        assert entry.binding.owner_version == pin["version"]
        assert descriptor["version"] != entry.binding.owner_version


@pytest.mark.parametrize(
    "change",
    [
        {"independent_review_sha256": "0" * 64},
        {"owner_version": "0.8.0.dev1"},
        {"owner_version": "0.10.0.dev1"},
        {"owner_commit": "e72f7136bb7c60cc90d170ed07e2753f29935b18"},
        {
            "owner_runtime_sha256": "90c86a19c1fdd6af7482322451c86972b8a628bc2b7ed8ea9a38f5e08b141111"
        },
    ],
)
def test_receipt_and_exact_owner_pin_cannot_be_rebound(owner_store, change):
    path, dataset = owner_store
    binding = bind_dataset(Store(path), dataset)
    first = binding.entries[0]
    changed = first.model_copy(update={"binding": first.binding.model_copy(update=change)})
    with pytest.raises(DomainError) as exc:
        binding.model_copy(update={"entries": (changed,) + binding.entries[1:]}).checked()
    assert exc.value.code == "M2_OWNER_BINDING_MISMATCH"


def test_scoped_review_keeps_original_envelope_and_quality(owner_store):
    path, dataset = owner_store
    binding = bind_dataset(Store(path), dataset)
    assert binding.dataset_id == dataset
    for entry in binding.entries:
        identity = window(binding, entry.owner_binding.owner_window_id).data_identity
        review = identity.owner_contract_review
        assert review.model_dump(mode="json") == source_pin()["contract_review"]
        assert entry.binding.independent_review_sha256 == review.receipt_sha256
        assert not review.execution_permission
        envelope = json.loads(entry.owner_binding.envelope)
        assert envelope["external_product_review"] == "PENDING"
        assert envelope["backend_execution_authorized"] is False
        assert identity.original_quality == envelope["original_quality"]
        assert identity.original_quality["finality"] == "UNVERIFIED"
        assert identity.original_quality["available_at"] is None
        assert identity.original_quality["historical_pit_verified"] is False
        assert identity.original_quality["verified_absent"] is False
        assert identity.external_owner_review_status == (
            "local_contract_verified_not_independently_reviewed"
        )
