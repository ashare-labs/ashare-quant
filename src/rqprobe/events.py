"""Replay of immutable, already published provider events; not a wall-clock feed."""
import json
from datetime import datetime
from rqalpha.interface import AbstractEventSource
from rqalpha.core.events import EVENT, Event
from .checkpoint import kill_at


class RecordedEventSource(AbstractEventSource):
    def __init__(self, env, records):
        self.env, self.records, self.cursor = env, records, 0
        if [r['cursor'] for r in records] != list(range(1, len(records)+1)):
            raise ValueError('NONCONTIGUOUS_INPUT_CURSOR')
    def get_state(self): return json.dumps({'cursor':self.cursor}).encode()
    def set_state(self, state): self.cursor = json.loads(state)['cursor']
    def events(self, start_date, end_date, frequency):
        for record in self.records[self.cursor:]:
            dt = datetime.fromisoformat(record['dt'])
            yield Event(EVENT[record['event']], calendar_dt=dt, trading_dt=dt)
            # Called only once Executor has finished the entire event including
            # strategy, broker, account and POST_* subscribers.
            self.cursor = record['cursor']
            kill_at('after_event', self.cursor)
            if self.env.config.base.persist:
                self.env.persist_helper.persist()
