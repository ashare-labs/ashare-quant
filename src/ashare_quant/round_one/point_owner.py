"""Composite owner references; TDX points never become strategy quote/bar values."""

import hashlib
import json
from pathlib import Path

from ashare_quant.execution_six.contracts import Read, owner_price
from ashare_quant.execution_six.owner import PublicOwner as Transport
from ashare_quant.execution_six.owner import share_count

from .owner import PublicOwner


class PointReader(Transport):
    worker_module = "ashare_quant.round_one.point_worker"

    @staticmethod
    def get_pin():
        return json.loads(Path(__file__).with_name("point-owner-pin.json").read_text())

    def __init__(self, binding, python):
        raw = json.loads(Path(binding).read_text())
        fixed = self.get_pin()
        if (raw["schema"], raw["ad_version"], raw["dataset_id"], raw["wheel"]["sha256"]) != (
            "ad.private-tdx-point-binding.v1",
            fixed["version"],
            fixed["dataset_id"],
            fixed["wheel_sha256"],
        ):
            raise RuntimeError("POINT_DELIVERY_PIN_MISMATCH")
        if not (
            raw["strategy_visible"] is False
            and raw["historical_pit"] is False
            and raw["no_ohlcv_synthesis"] is True
        ):
            raise RuntimeError("POINT_ROLE_CONTRACT_CHANGED")
        self.binding = dict(store_path=raw["owner_store"], dataset_id=raw["dataset_id"])
        self.binding_sha256 = hashlib.sha256(Path(binding).read_bytes()).hexdigest()
        self.python, self.cache = str(Path(python).absolute()), {}
        self.raw = raw

    def descriptor(self):
        return dict(
            dataset_id=self.binding["dataset_id"],
            binding_sha256=self.binding_sha256,
            owner_pin=self.get_pin(),
            historical_pit=False,
            strategy_visible=False,
            limitations=self.raw["missing"],
            label_basis="request date and protocol index",
        )

    def point(self, security, at):
        result = self.call(
            dict(
                operation="read",
                parameters=dict(
                    security=security, at=at.isoformat(), role="retrospective_point_research"
                ),
            )
        )
        if result["status"] != "available" or len(result["rows"]) != 1:
            raise RuntimeError("POINT_REQUIRED_OBSERVATION_UNAVAILABLE")
        row = result["rows"][0]
        if (
            row["security"],
            row["source_label_at"],
            row["strategy_visible"],
            result["historical_pit"],
        ) != (security, at.isoformat(), False, False):
            raise RuntimeError("POINT_CONTEXT_OR_VISIBILITY_MISMATCH")
        owner_price(row["values"]["price"])
        shares = share_count(row["values"]["volume_shares"])
        if shares != share_count(row["values"]["volume_lots"]) * 100:
            raise RuntimeError("POINT_VOLUME_UNIT_MISMATCH")
        return result


class PointOwner(PublicOwner):
    def __init__(
        self, binding, owner_python, case_id, *, point_binding, point_owner_python, **kwargs
    ):
        super().__init__(binding, owner_python, case_id, **kwargs)
        self.points = PointReader(point_binding, point_owner_python)
        self.model = json.loads(Path(__file__).with_name("point-model.json").read_text())

    def descriptor(self):
        value = super().descriptor()
        value.update(
            point_provider=self.points.descriptor(),
            execution_model=self.model,
            source_combination="JQ observed request quote plus independent TDX retrospective points",
        )
        return value

    def read(self, operation, logical_at, **parameters):
        if operation != "point":
            return super().read(operation, logical_at, **parameters)
        if (
            parameters.get("consumer") != "engine"
            or logical_at.date() != self.case.day
            or logical_at.strftime("%H:%M:%S") not in {"13:01:00", "15:00:00"}
        ):
            raise RuntimeError("POINT_ENGINE_CLOCK_ROLE_DENIED")
        public = self.points.point(self.case.security, logical_at)
        value = public["rows"][0]["values"]
        return Read.of(
            operation,
            logical_at,
            dict(price=value["price"], volume=share_count(value["volume_shares"])),
            dict(
                public_result=public,
                consumer_model=self.model["model_id"],
                strategy_visible=False,
                historical_pit=False,
                consumer="engine",
            ),
        )
