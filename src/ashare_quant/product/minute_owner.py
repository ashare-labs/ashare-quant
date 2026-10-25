"""Keep AD's loaded deployment separate from its derived minute-reader assets."""

from copy import deepcopy

from ashare_data.product import LocalMinuteProduct


class MinuteOwner(LocalMinuteProduct):
    @property
    def document(self):
        return self._document

    @document.setter
    def document(self, value):
        # The pinned AD constructor assigns its loaded manifest before merging
        # frozen release assets into it. Capture that same read, not a second
        # file read that could miss a deployment changed and then restored.
        self.deployment_document = deepcopy(value)
        self._document = value
