"""Single trusted host lifetime; observe native lifecycle without replacing RQ code.

CPython 3.12 local PY_START events catch A -> B -> A even if no adapter query
occurs while B is active. This is an ownership guard, not a hostile-code sandbox.
"""

import importlib
import sys
import threading
import weakref
from uuid import uuid4

from rqalpha.environment import Environment

from .d1_rq_adapter import stop

_TOOL = 4
_NAME = "ashare-quant-bounded-lifecycle"
_codes = None
_active = None
_claimed = weakref.WeakSet()
_start_witness = 0
_call_witness = 0
_register = sys.monitoring.register_callback
_set_events = sys.monitoring.set_events
_set_local = sys.monitoring.set_local_events
_free_tool = sys.monitoring.free_tool_id


def _monitor_change(operation, *args):
    """Only this private call site may maintain our observer without revocation."""
    return operation(*args)


def _observer_probe():
    """No engine access: both registered callbacks must witness this invocation."""


def _active_lifetime():
    return _active() if _active is not None else None


def _guard_monitor_change(code, offset, function, first_arg):
    global _call_witness
    if function is _observer_probe:
        _call_witness += 1
    elif (
        (
            function is _register
            or function is _set_events
            or function is _set_local
            or function is _free_tool
        )
        and code is not _monitor_change.__code__
        and (first_arg == _TOOL or first_arg is sys.monitoring.MISSING)
    ):
        active = _active_lifetime()
        if active is not None:
            # CALL fires before the C operation, including removal of this very
            # callback. Restore-before-query therefore cannot resurrect a lease.
            active.close("lifecycle monitoring configuration changed")


def _native_codes():
    return (
        Environment.__init__.__code__,
        importlib.import_module("rqalpha.main").cleanup_resources.__code__,
    )


def _revoke_on_native_entry(code, offset):
    global _start_witness
    if code is _observer_probe.__code__:
        _start_witness += 1
        return
    active = _active_lifetime()
    if active is not None:
        active.close("native Environment creation or cleanup")


def _observe():
    global _codes
    if _codes is None:
        if not hasattr(sys, "monitoring") or sys.monitoring.get_tool(_TOOL) is not None:
            stop("exclusive lifecycle observer unavailable")
        _codes = _native_codes()
        sys.monitoring.use_tool_id(_TOOL, _NAME)
        _monitor_change(_register, _TOOL, sys.monitoring.events.PY_START, _revoke_on_native_entry)
        _monitor_change(_register, _TOOL, sys.monitoring.events.CALL, _guard_monitor_change)
        for code in (*_codes, _observer_probe.__code__):
            _monitor_change(_set_local, _TOOL, code, sys.monitoring.events.PY_START)
        _monitor_change(_set_events, _TOOL, sys.monitoring.events.CALL)
    _verify_observer()


def _verify_observer():
    if (
        sys.monitoring.get_tool(_TOOL) != _NAME
        or sys.monitoring.get_events(_TOOL) != sys.monitoring.events.CALL
        or _native_codes() != _codes
        or any(
            sys.monitoring.get_local_events(_TOOL, c) != sys.monitoring.events.PY_START
            for c in (*_codes, _observer_probe.__code__)
        )
    ):
        stop("lifecycle observer configuration unavailable")
    for event, expected in (
        (sys.monitoring.events.PY_START, _revoke_on_native_entry),
        (sys.monitoring.events.CALL, _guard_monitor_change),
    ):
        previous = _monitor_change(_register, _TOOL, event, expected)
        if previous is not expected:
            _monitor_change(_register, _TOOL, event, previous)
            stop("lifecycle callback ownership lost")
    before = (_start_witness, _call_witness)
    _observer_probe()
    if (_start_witness, _call_witness) != (before[0] + 1, before[1] + 1):
        stop("lifecycle callbacks did not execute")


class HostLifetime:
    def __init__(self, env):
        global _active
        _observe()
        if type(env) is not Environment or Environment.get_instance() is not env or env in _claimed:
            stop("fresh active native Environment required")
        previous = _active() if _active is not None else None
        if previous is not None:
            previous.close("new host claimed")
        _claimed.add(env)
        _active = weakref.ref(self)
        self.env, self.epoch = env, uuid4().hex
        self.thread = threading.get_ident()
        self.reason = None
        self.parts = {
            name: getattr(env, name)
            for name in ("config", "event_source", "event_bus", "_universe")
        }
        self.stage = "CREATED"
        self.source = self.board = self.proxy = None
        self.events = None

    def close(self, reason="host closed"):
        if self.reason is None:
            self.reason = reason
            events = getattr(self, "events", None)
            if events is not None:
                events.detach()

    def reject(self, reason):
        self.close(reason)
        stop(reason)

    def check(self, *, installed=False):
        if self.reason is not None:
            stop("host permanently revoked: " + self.reason)
        try:
            _verify_observer()
        except Exception:
            self.close("lifecycle observer integrity lost")
            raise
        if (
            threading.get_ident() != self.thread
            or _active is None
            or _active() is not self
            or Environment._env is not self.env
            or any(getattr(self.env, k, None) is not v for k, v in self.parts.items())
            or getattr(self.env.event_source, "env", None) is not self.env
        ):
            self.reject("native lifecycle or component ownership changed")
        if self.events is not None:
            self.events.check()
        for key, value in (("data_source", self.source), ("price_board", self.board)):
            if getattr(self.env, key, None) is not value:
                self.reject("native data component replaced")
        current = getattr(self.env, "data_proxy", None)
        if self.proxy is not None and current is self.proxy:
            self.stage = "ACTIVE"
        elif current is not None or self.stage == "ACTIVE":
            self.reject("native proxy replaced or removed")
        if self.source is not None and (
            self.source.boundary.life is not self or self.board._ds is not self.source
        ):
            self.reject("source/board ownership changed")
        if self.proxy is not None and (
            self.proxy.boundary.life is not self
            or self.proxy._data_source is not self.source
            or self.proxy._price_board is not self.board
        ):
            self.reject("proxy ownership changed")
        if installed and self.stage != "ACTIVE":
            stop("data proxy must be installed before state access")

    def bind_data(self, source, board):
        self.check()
        if self.stage != "CREATED":
            self.reject("host data binding is single use")
        self.source, self.board = source, board
        self.env.set_data_source(source)
        self.env.set_price_board(board)
        self.stage = "DATA"
        self.check()

    def begin_proxy(self, proxy, source, board):
        self.check()
        if self.stage != "DATA" or source is not self.source or board is not self.board:
            self.reject("proxy construction is single use with owned components")
        self.proxy, self.stage = proxy, "BUILDING"

    def finish_proxy(self):
        self.stage = "READY"
        self.check()
