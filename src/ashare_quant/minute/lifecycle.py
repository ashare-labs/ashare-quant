"""JQ minute callback clocks, separate from native closed-bar matching events."""

from datetime import timedelta

from rqalpha.core.events import EVENT, Event
from rqalpha.interface import AbstractEventSource

from .inputs import require, stamp

MODEL = "jq_minute"


def handle_clocks(day):
    return tuple(
        stamp(day + "T" + clock + ":00+08:00") + timedelta(minutes=n)
        for clock in ("09:30", "13:00")
        for n in range(120)
    )


def schedule_clock(clock):
    if clock in {"09:00", "15:30", "every_bar"}:
        return True
    try:
        parsed = stamp("2000-01-01T" + clock + ":00+08:00")
    except (TypeError, ValueError):
        return False
    return parsed.strftime("%H:%M") == clock and "09:30" <= clock <= "15:00"


def validate_sessions(data):
    for day in data.days:
        expected = [
            (stamp(day + "T09:30:00+08:00"), stamp(day + "T11:30:00+08:00")),
            (stamp(day + "T13:00:00+08:00"), stamp(day + "T15:00:00+08:00")),
        ]
        require(data.sessions[day] == expected, "JQ_LIFECYCLE_REQUIRES_FULL_AD_SESSIONS")


class JQEventSource(AbstractEventSource):
    """Clock events carry no OHLC; RQAlpha still dispatches every native BAR.

    Scheduled callbacks precede pending-order matching. Handle callbacks follow
    that matching, except 09:30/13:00 which have no just-completed minute BAR.
    The private USER event is used only by this minute adapter, not the task DB.
    """

    def __init__(self, data):
        self.data = data

    @staticmethod
    def event(kind, at, phase=None):
        dt = at.replace(tzinfo=None)
        return Event(kind, calendar_dt=dt, trading_dt=dt, minute_phase=phase)

    def events(self, start_date, end_date, frequency):
        for day in self.data.days:
            yield self.event(EVENT.BEFORE_TRADING, stamp(day + "T09:00:00+08:00"))
            start = stamp(day + "T09:30:00+08:00")
            ends = set(self.data.ends[day])
            for minute in range(331):
                at = start + timedelta(minutes=minute)
                yield self.event(EVENT.USER, at, "scheduled")
                if at in ends:
                    yield self.event(EVENT.BAR, at)
            # A USER event deliberately avoids SimulationBroker.after_trading's
            # unverified 15:30 expiry. Existing orders expire at the explicitly
            # modeled next-session boundary, before that day's user callbacks.
            yield self.event(EVENT.USER, stamp(day + "T15:30:00+08:00"), "after")
        yield self.event(EVENT.USER, stamp(self.data.days[-1] + "T23:59:59.001000+08:00"), "end")

    def get_state(self):
        raise RuntimeError("JQ_LIFECYCLE_INTERMEDIATE_RESTORE_UNSUPPORTED")

    def set_state(self, state):
        raise RuntimeError("JQ_LIFECYCLE_INTERMEDIATE_RESTORE_UNSUPPORTED")
