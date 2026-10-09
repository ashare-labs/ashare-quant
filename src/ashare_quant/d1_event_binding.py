"""Own four native PRE-event listener slots without changing RQ dispatch code."""

from collections.abc import MutableMapping, MutableSequence


class _PhaseListeners(MutableSequence):
    def __init__(self, life, callback, others):
        self.life, self.callback = life, callback
        self._items = [callback, *others]

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def _replace(self, changed):
        if (
            not changed
            or changed[0] is not self.callback
            or sum(item is self.callback for item in changed) != 1
        ):
            self.life.reject("owned phase callback removed, replaced or reordered")
        self._items = changed

    def __setitem__(self, index, value):
        changed = self._items.copy()
        changed[index] = value
        self._replace(changed)

    def __delitem__(self, index):
        changed = self._items.copy()
        del changed[index]
        self._replace(changed)

    def insert(self, index, value):
        changed = self._items.copy()
        changed.insert(index, value)
        self._replace(changed)


class EventBindings(MutableMapping):
    def __init__(self, life, callbacks):
        self.life, self.bus = life, life.env.event_bus
        self.other = self.bus._listeners
        self.owned = {
            event: _PhaseListeners(life, callback, self.other[event])
            for event, callback in callbacks.items()
        }
        self.callbacks = callbacks.copy()
        self.detached = False
        self.bus._listeners = self
        life.events = self

    def __getitem__(self, key):
        return self.owned[key] if key in self.owned else self.other[key]

    def __setitem__(self, key, value):
        if key in self.owned:
            self.life.reject("owned phase listener slot rebound")
        self.other[key] = value

    def __delitem__(self, key):
        if key in self.owned:
            self.life.reject("owned phase listener slot unbound")
        del self.other[key]

    def __iter__(self):
        return iter(dict.fromkeys((*self.other, *self.owned)))

    def __len__(self):
        return len(set(self.other) | set(self.owned))

    def check(self):
        if self.detached or self.bus._listeners is not self:
            self.life.reject("native phase listener registry replaced")
        for event, callback in self.callbacks.items():
            sequence = self.owned.get(event)
            if (
                not isinstance(sequence, _PhaseListeners)
                or sequence.life is not self.life
                or sequence.callback is not callback
                or not sequence
                or sequence[0] is not callback
                or sum(item is callback for item in sequence) != 1
            ):
                self.life.reject("native phase callback ownership changed")

    def detach(self):
        """Release owned slots only after the lifetime is permanently closed."""
        if self.detached:
            return
        self.detached = True
        if self.bus._listeners is self:
            for event, sequence in self.owned.items():
                self.other[event] = [item for item in sequence if item is not self.callbacks[event]]
            self.bus._listeners = self.other
