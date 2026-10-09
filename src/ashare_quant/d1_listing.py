"""Public dev3 owner binding. No lifecycle facts are generated here."""

import hashlib
import json
from importlib.resources import files

from .d1_br1 import profile_sha256
from .d1_codec import FrozenBR1Reader, owner_pin, public_digest
from .d1_rq_adapter import SID, stop


def listing_pin():
    pin = json.loads(files("ashare_quant").joinpath("d1-listing-pin.json").read_text())
    owner = owner_pin()
    if (
        pin["listing_review_receipt_sha256"] != owner["listing_review_receipt_sha256"]
        or pin["contract_sha256"] != owner["listing_contract_sha256"]
        or pin["data_wheel_sha256"] != owner["data_wheel_sha256"]
    ):
        stop("listing review binding")
    return pin


class ListingOwner:
    def __init__(self, root):
        self.reader = FrozenBR1Reader(
            root,
            dataset_id=owner_pin()["br1_dataset_id"],
            mode="conditional_research",
            backend_profile_sha256=profile_sha256(),
        )
        self.snapshot = self.reader.snapshot()
        self.pin = listing_pin()
        self.view = self.reader._call(
            lambda: (
                self.reader._store()
                .br1(owner_pin()["br1_dataset_id"], mode="conditional_research")
                .listing(contract_sha256=self.pin["contract_sha256"])
            )
        )
        self.contract = self.descriptor()
        self.contract_digest = public_digest(self.contract.to_dict())

    def descriptor(self):
        c = self.reader._call(self.view.descriptor)
        if (
            c.contract_sha256 != self.pin["contract_sha256"]
            or hashlib.sha256(c.raw_json.encode()).hexdigest() != c.contract_sha256
            or c.policy_sha256 != self.pin["policy_sha256"]
            or c.dataset_id != owner_pin()["br1_dataset_id"]
            or c.profile_sha256 != owner_pin()["owner_profile_sha256"]
            or c.execution_permission is not False
        ):
            stop("listing contract identity")
        if hasattr(self, "contract_digest") and public_digest(c.to_dict()) != self.contract_digest:
            stop("listing contract drift")
        return c

    def query(self, *, current, target, role, context, successor=False, n=1):
        self.descriptor()
        request = dict(
            security=SID,
            current_date=current,
            target_date=target,
            role=role,
            context_sha256=context,
        )
        if successor:
            value = self.reader._call(
                lambda: self.view.successor(
                    security=SID,
                    current_date=current,
                    candidate_date=target,
                    n=n,
                    context_sha256=context,
                )
            )
        else:
            value = self.reader._call(lambda: self.view.status(**request))
        value = self.reader._call(lambda: self.view.revalidate(value, **request))
        if (
            value.model_listed is not True
            or value.model_delisted is not False
            or value.absolute_delisting_date is not None
            or value.absolute_date_status != "unknown"
            or value.execution_permission is not False
            or value.assumption_ids != ("A-NORMAL",)
        ):
            stop("listing projection conflict")
        return value
