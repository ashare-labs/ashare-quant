"""Prepared RQ history_bars seam; direct codec probes are NOT native AD08 evidence.

No RQ event loop, orders, account or native checkpoint writer is constructed here.
"""

from dataclasses import dataclass
from datetime import datetime, time

import numpy as np
from ashare_data.research.portable_codec import consume_previous, history_array, native_ad08_call

from ashare_quant.backtests.daily.facade import NativeObservation
from ashare_quant.contracts import SHANGHAI
from ashare_quant.providers.daily.owner import require_backend_execution
from ashare_quant.providers.daily.owner_wire import owner_call, pack, receipt_projection, unpack
from ashare_quant.storage.canonical import require


@dataclass(frozen=True)
class AD08Plan:
    trading_date: str
    previous_date: str
    getter: str = "DataProxy.get_prev_close(600000.XSHG, T09:00)"
    call_chain: tuple[str, ...] = ("get_prev_close", "_get_prev_close", "data_source.history_bars", "owner.decision_prev_close")
    time_policy: str = "rq641_shanghai_midnight"
    before_trading: str = "09:00 Asia/Shanghai; host DECIDE subphase"
    bar: str = "15:00 Asia/Shanghai; CURRENT_BAR_CLOSE"
    native_call_observed: bool = False
    execution_permission: bool = False


class PreparedRQBridge:
    def __init__(self, client, current):
        client.context(current)
        self.client, self.current = client, current
        self.read = None
        self.history_attempts = 0

    def plan(self):
        return AD08Plan(self.current.trading_date.isoformat(), self.current.previous_open.isoformat())

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True,
                     include_now=False, adjust_type="pre", adjust_orig=None):
        self.client.context(self.current)
        call = owner_call(lambda: native_ad08_call(security=instrument.order_book_id,
            trade_date=self.current.trading_date, dt=dt, adjust_orig=adjust_orig, frequency=frequency,
            fields=fields, bar_count=bar_count, include_now=include_now, skip_suspended=skip_suspended,
            adjust_type=adjust_type, time_policy="rq641_shanghai_midnight"))
        self.history_attempts += 1
        read = self.client.previous(self.current, call)
        array = owner_call(lambda: history_array(read, use=self.client.use, context=self.client.context(self.current)))
        self.read = read
        return array

    def capture_return(self, native_value, *, origin=None):
        require(self.read is not None, "M2_OWNER_DOCUMENTS_REQUIRED", "Capture requires a verified current history read or cache authorization")
        context = self.client.context(self.current)
        require(type(native_value) in (float, np.float64), "M2_NATIVE_DATAPROXY_TYPE", "DataProxy return must be a float64 scalar")
        consumed = owner_call(lambda: consume_previous(native_value, read=self.read, use=self.client.use, context=context))
        require(consumed.codec_observation.to_dict()["native_call_observed"] is False,
                "M2_NATIVE_CLAIM_INVALID", "Prepared codec cannot claim native invocation")
        receipt = receipt_projection(self.read, self.client.session.entry, self.current)
        return NativeObservation(native_value, receipt.call,
            origin.native_call_id if origin else f"codec-probe-{self.current.event_cursor}-{self.current.generation}",
            receipt, origin_fact=origin, owner_documents=pack(self.read),
            codec_observation=consumed.codec_observation.data.decode())

    def authorize_cache(self, origin):
        require(origin.owner_documents is not None, "M2_OWNER_DOCUMENTS_REQUIRED", "Cache must carry the entire first owner read")
        self.read = self.client.reauthorize(unpack(origin.owner_documents), origin.origin_context, self.current)

    def start_native_run(self):
        require_backend_execution()  # First operation; no RQ instance or account is constructed.


class CodecProbePort:
    """Explicit non-native driver to test owner conversion + decision persistence."""
    def __init__(self, bridge, *, origin=None, native_value=None):
        self.bridge, self.origin, self.native_value = bridge, origin, native_value

    def current_context(self):
        return self.bridge.current

    def read(self, context, call):
        require(context == self.bridge.current, "M2_READ_AUTHORITY_MISMATCH", "Probe context changed")
        if self.origin is not None:
            self.bridge.authorize_cache(self.origin)
            value = float(self.origin.native_value)
        else:
            instrument = type("ContractInstrument", (), {"order_book_id": call.security})()
            # Exercise the exact public RQ-style signature; this is not an actual DataProxy call.
            value = self.bridge.history_bars(instrument, call.count, call.frequency, call.field,
                datetime.combine(context.previous_open, time(), SHANGHAI), skip_suspended=False, include_now=False,
                adjust_type=call.adjustment_requested, adjust_orig=datetime.combine(context.trading_date, time(), SHANGHAI))[0]
        return self.bridge.capture_return(value if self.native_value is None else self.native_value, origin=self.origin)
