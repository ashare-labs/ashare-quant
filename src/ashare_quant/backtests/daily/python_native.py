"""Use the existing daily source/matcher with the shared Python callback host."""

from ashare_quant.portfolio.native import Matcher as PortfolioMatcher
from ashare_quant.portfolio.native import Source as PortfolioSource


class Source(PortfolioSource):
    def is_suspended(self, sid, dates):
        return [
            self.host.data.is_paused(sid, str(d.date() if hasattr(d, "date") else d))
            for d in dates
        ]


class Matcher(PortfolioMatcher):
    execution_phase = "bar"

    def volume_ratio(self):
        return self.host.config["volume_percent"]
