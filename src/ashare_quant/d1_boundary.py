"""Finite runtime authority for the trusted, fixed BR1 host; no account arithmetic."""

import json
from datetime import datetime, time
from uuid import uuid4

from rqprobe.runtime import canonical, runtime_identity

from .contracts import SHANGHAI
from .d1_codec import owner_pin, public_digest
from .d1_lifecycle import HostLifetime
from .d1_listing import listing_pin
from .d1_rq_adapter import SID, model_datetime, stop
from .identity import source_inventory


def fixed_records(contract):
    return [
        {
            "cursor": index * 3 + offset,
            "event": event,
            "dt": datetime.combine(day, clock).isoformat(),
            "clock_kind": "declared_daily_model_schedule_not_market_receipt",
        }
        for index, day in enumerate(contract.event_dates)
        for offset, event, clock in (
            (1, "BEFORE_TRADING", time(8, 30)),
            (2, "BAR", time(15)),
            (3, "AFTER_TRADING", time(15, 30)),
        )
    ]


class NativeBoundary:
    def __init__(self, env, owner, *, run_identity):
        if len(run_identity) != 64 or any(c not in "0123456789abcdef" for c in run_identity):
            stop("run identity")
        self.env, self.owner, self.run_identity = env, owner, run_identity
        self._initial_run_identity = run_identity
        self.records = fixed_records(owner.contract)
        self._sources = source_inventory()
        self._rq = runtime_identity()
        self._config = self.config_snapshot()
        self._pins = {"owner": owner_pin(), "listing": listing_pin()}
        self.identity = public_digest(
            {
                "sources": self._sources,
                "rq": self._rq,
                "config": self._config,
                "pins": self._pins,
                "run": run_identity,
                "contract": owner.contract_digest,
                "records": self.records,
            }
        )
        self.phase, self.generation = "BOOT", 0
        self._restore_digest = None
        self._at = datetime.combine(owner.contract.event_dates[0], time())
        self._source = None
        self._validate_config()
        self.life = HostLifetime(env)
        self._attempt = None
        self._restore_pending = False

    def config_snapshot(self):
        raw = self.env.config.convert_to_dict()
        calendar = raw["base"].pop("trading_calendar", None)
        if calendar is not None:
            expected = [
                datetime.combine(d, time()).isoformat() for d in self.owner.contract.event_dates
            ]
            try:
                actual = [d.isoformat() for d in calendar]
            except (AttributeError, TypeError):
                stop("derived native calendar encoding")
            if actual != expected:
                stop("derived native calendar changed")
        result = canonical(raw)
        # Exact native normalization only; no next_bar or other mode is accepted.
        if result["mod"]["sys_simulation"]["matching_type"] == "CURRENT_BAR_CLOSE":
            result["mod"]["sys_simulation"]["matching_type"] = "current_bar"
        return result

    def _validate_config(self):
        c = self._config
        base, mods = c["base"], c["mod"]
        days = [d.isoformat() for d in self.owner.contract.event_dates]
        if (
            base["start_date"] != days[0]
            or base["end_date"] != days[-1]
            or base["frequency"] != "1d"
            or base["accounts"] != {"STOCK": 10000.0}
            or base.get("init_positions") not in ([], None)
            or base.get("benchmark")
            or c.get("extra", {}).get("benchmark")
            or c.get("extra", {}).get("enable_profiler", False)
            or mods["sys_analyser"].get("enabled", False)
        ):
            stop("fixed native configuration")
        base_required = {
            "run_type": "BACKTEST",
            "market": "CN",
            "persist": True,
            "persist_mode": "REAL_TIME",
            "rqdatac_uri": "disabled",
            "auto_update_bundle": False,
            "capital_gain_tax_rate": 0,
            "partial_fill_on_insufficient_cash": False,
        }
        if any(base.get(k) != v for k, v in base_required.items()):
            stop("fixed native base configuration")
        required = {
            "sys_accounts": {"stock_t1": True, "validate_stock_position": True},
            "sys_risk": {
                "validate_cash": True,
                "validate_price": True,
                "validate_is_trading": True,
            },
            "sys_simulation": {
                "price_limit": True,
                "volume_limit": True,
                "inactive_limit": True,
                "matching_type": "current_bar",
                "slippage": 0,
                "volume_percent": 0.01,
                "signal": False,
                "management_fee": [],
            },
            "sys_transaction_cost": {
                "stock_min_commission": 5,
                "stock_commission_multiplier": 1,
                "tax_multiplier": 1,
                "pit_tax": True,
            },
        }
        if any(mods[m].get(k) != v for m, values in required.items() for k, v in values.items()):
            stop("required native checks")

    def check_identity(self):
        self.life.check()
        if (
            source_inventory() != self._sources
            or runtime_identity() != self._rq
            or self.run_identity != self._initial_run_identity
            or public_digest(self.owner.contract.to_dict()) != self.owner.contract_digest
            or {"owner": owner_pin(), "listing": listing_pin()} != self._pins
            or self.config_snapshot() != self._config
            or self.env.event_source.records != self.records
        ):
            stop("runtime binding drift")
        self.owner.descriptor()

    def _clock(self):
        at, trading = model_datetime(self.env.calendar_dt), model_datetime(self.env.trading_dt)
        if at != trading:
            stop("calendar/trading clock disagreement")
        return at

    def _check_position(self, phase, at, cursor):
        if type(cursor) is not int:
            stop("cursor type")
        if phase == "BOOT":
            if at != datetime.combine(self.owner.contract.event_dates[0], time()) or cursor != 0:
                stop("initial clock/cursor")
            return
        if phase == "WARMUP":
            allowed = [(c, t) for r, c, t in self.owner.contract.allowed_requests if r == "warmup"]
            if (at.date(), at.date()) not in allowed or at.time() != time() or cursor != 0:
                stop("warmup clock/cursor")
            return
        if at.date() not in self.owner.contract.event_dates:
            stop("event date outside fixed scene")
        index = self.owner.contract.event_dates.index(at.date()) * 3
        clocks = {
            "BEFORE_TRADING": (time(8, 30), {index, index + 1}),
            "BAR": (time(15), {index + 1, index + 2}),
            "AFTER_TRADING": (time(15, 30), {index + 2, index + 3}),
            "SETTLEMENT": (time(15, 30), {index + 3}),
        }
        if phase not in clocks or at.time() != clocks[phase][0] or cursor not in clocks[phase][1]:
            stop("phase/clock/cursor mismatch")

    def context(self):
        self.check_identity()
        self.life.check(installed=True)
        at = self._clock()
        if at != self._at:
            stop("uncontrolled clock advance")
        self._check_position(self.phase, at, self.env.event_source.cursor)
        if self.phase not in {"BOOT", "WARMUP"} and set(self.env.get_universe()) != {SID}:
            stop("fixed universe mismatch")
        return public_digest(
            {
                "identity": self.identity,
                "run": self.run_identity,
                "phase": self.phase,
                "at": at.replace(tzinfo=SHANGHAI).isoformat(),
                "raw_calendar": self.env.calendar_dt.isoformat(),
                "raw_trading": self.env.trading_dt.isoformat(),
                "cursor": self.env.event_source.cursor,
                "generation": self.generation,
                "runtime_epoch": self.life.epoch,
                "restore_attempt": self._attempt,
                "restore_pending": self._restore_pending,
            }
        )

    def transition(self, phase):
        self.check_identity()
        self.life.check(installed=True)
        if self._restore_pending:
            stop("restore pending before native event")
        at, cursor = self._clock(), self.env.event_source.cursor
        self._check_position(phase, at, cursor)
        sequence = [("BOOT", self.owner.contract.event_dates[0], 0)]
        for index, day in enumerate(self.owner.contract.event_dates):
            sequence.extend(
                (p, day, index * 3 + offset)
                for p, offset in (
                    ("BEFORE_TRADING", 0),
                    ("BAR", 1),
                    ("AFTER_TRADING", 2),
                    ("SETTLEMENT", 3),
                )
            )
        previous = next(
            (i for i, (p, d, _) in enumerate(sequence) if (p, d) == (self.phase, self._at.date())),
            -1,
        )
        if (
            previous < 0
            or previous + 1 >= len(sequence)
            or (phase, at.date(), cursor) != sequence[previous + 1]
        ):
            stop("missing or skipped native event")
        prior = {
            "BEFORE_TRADING": {"BOOT", "SETTLEMENT"},
            "BAR": {"BEFORE_TRADING"},
            "AFTER_TRADING": {"BAR"},
            "SETTLEMENT": {"AFTER_TRADING"},
        }
        if phase not in prior or self.phase not in prior[phase] or at < self._at:
            stop("event phase sequence")
        if set(self.env.get_universe()) != {SID}:
            stop("fixed universe mismatch")
        self.phase, self._at = phase, at
        if self._source is not None:
            self._source.sync_phase()
        self.context()

    def projection(self, target, *, successor=False, n=1):
        before = self.context()
        current = self._at.date()
        if self.phase == "WARMUP":
            role = "warmup"
        elif target == current and not successor:
            role = "current"
        elif self.phase == "SETTLEMENT":
            role = "settlement_successor"
        else:
            stop("target role outside actual phase")
        value = self.owner.query(
            current=current, target=target, role=role, context=before, successor=successor, n=n
        )
        if self.context() != before:
            stop("runtime advanced during owner query")
        return value

    def get_state(self):
        self.context()
        if self._restore_pending:
            stop("cannot persist partially installed restore")
        return json.dumps(
            {
                "identity": self.identity,
                "phase": self.phase,
                "at": self._at.isoformat(),
                "cursor": self.env.event_source.cursor,
                "generation": self.generation,
            },
            sort_keys=True,
        ).encode()

    def validate_state(self, state, *, complete=False):
        if (
            type(state) is not dict
            or set(state) != {"identity", "phase", "at", "cursor", "generation"}
            or state["identity"] != self.identity
            or type(state["generation"]) is not int
            or state["generation"] < 0
        ):
            stop("checkpoint boundary identity")
        at = datetime.fromisoformat(state["at"])
        if at.tzinfo is not None:
            stop("checkpoint clock encoding")
        self._check_position(state["phase"], at, state["cursor"])
        if complete and (
            state["phase"] != "SETTLEMENT"
            or state["cursor"] != len(self.records)
            or at.date() != self.owner.contract.event_dates[-1]
        ):
            stop("completed checkpoint domain")
        # A state can only commit at complete input-event boundaries.
        if state["cursor"]:
            last = self.records[state["cursor"] - 1]
            if at.isoformat() != last["dt"] or (
                state["phase"] != last["event"] and state["phase"] != "SETTLEMENT"
            ):
                stop("checkpoint cursor/clock relation")
        elif state["phase"] != "BOOT":
            stop("checkpoint before first event")
        return at

    def validate_checkpoint(self, payload):
        self.check_identity()
        self.life.check(installed=True)
        if type(payload["complete"]) is not bool:
            stop("checkpoint completion type")
        state = json.loads(payload["states"]["mod_bounded"])
        at = self.validate_state(state, complete=payload["complete"])
        cursor = json.loads(payload["states"]["event_source"])["cursor"]
        clocks = payload["extras"]["clock"]
        if (
            type(cursor) is not int
            or cursor != state["cursor"]
            or len(clocks) != 2
            or any(not isinstance(c, datetime) or c.tzinfo is not None or c != at for c in clocks)
        ):
            stop("checkpoint native clock/cursor conflict")
        if json.loads(payload["states"]["universe"]) != [SID]:
            stop("checkpoint universe mismatch")
        if payload["complete"] and not isinstance(payload["receipt"], dict):
            stop("completed checkpoint missing receipt")
        return state

    def preflight_checkpoint(self, payload):
        self.check_identity()
        if self._restore_pending:
            self.life.reject("overlapping restore preflights are ambiguous")
        self._attempt = uuid4().hex
        self.generation += 1
        self._restore_digest = None
        try:
            state = self.validate_checkpoint(payload)
            self.generation = max(self.generation, state["generation"] + 1)
            self._restore_digest = public_digest(state)
            self._restore_pending = True
            return state
        except Exception:
            self.life.close("checkpoint preflight failed")
            raise

    def set_state(self, raw):
        self.check_identity()
        self.life.check(installed=True)
        digest = self._restore_digest
        self._restore_digest = None
        try:
            state = json.loads(raw)
            if not self._restore_pending or digest is None or public_digest(state) != digest:
                stop("restore must follow fresh checkpoint preflight")
            self._restore_pending = False
            at = self.validate_state(state)
            if self._clock() != at or self.env.event_source.cursor != state["cursor"]:
                stop("native restore order/clock mismatch")
            self.phase, self._at = state["phase"], at
            if self._source is not None:
                self._source.sync_phase(restoring=True)
            self.projection(at.date())  # Fresh public binding, never a restored query object.
        except Exception:
            self.life.close("metadata restore failed; new Environment required")
            raise

    def projection_metadata(self):
        self.context()
        return {
            "engine_projection_kind": "bounded_horizon_marker",
            "adapter_version": listing_pin()["adapter_version"],
            "contract_sha256": self.owner.contract.contract_sha256,
            "source_absolute_date_status": "unknown",
            "execution_permission": False,
        }
