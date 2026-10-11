from .fee_host import Host as FeeHost
from .fee_host import Mod
from .lifecycle_host import LifecycleAPI


class Host(FeeHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.api = LifecycleAPI(self)

    def after(self):
        super().after()
        self.api.fixed_queries()

    def complete(self):
        result = super().complete()
        result["scenario_triggered"] &= sum(e["kind"] == "query_return" for e in self.records) == 1
        result["status"] = (
            "SCENARIO_EXECUTED" if result["scenario_triggered"] else "SCENARIO_NOT_TRIGGERED"
        )
        return result


def load_mod():
    return Mod()
