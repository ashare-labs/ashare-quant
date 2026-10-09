"""Restore fixed metadata before native objects that query that metadata."""
from rqprobe.checkpoint import StrictHelper


class FixedHelper(StrictHelper):
    def restore(self, event=None):
        first = ("universe", "event_source", "mod_bounded")
        if any(key not in self.objects for key in first):
            raise RuntimeError("FIXED_RESTORE_REGISTRY_MISSING")
        # Strategy init has already subscribed the exact fixed universe. Restoring
        # the same universe emits no change. Clock is installed by StrictHelper;
        # then cursor and checked boundary must precede native position setters.
        self.objects = {**{k: self.objects[k] for k in first}, **self.objects}
        return super().restore(event)
