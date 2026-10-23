"""Actual A数达 Client price seam. It cannot admit a full engine run at dev2.

Calendar/master/actions/limits/status assets must be supplied by A数达 itself;
this module does not reconstruct them. Methods intentionally stay explicit.
"""
from ashare_quant.execution.rqalpha.provider import CapabilityError
from ashare_quant.execution.rqalpha.provider import aware


class AsharePriceBridge:
    def __init__(self, client, *, visibility):
        self.client, self.visibility = client, visibility
        self.context, self.as_of = None, None

    def pin(self, strategy_clock):
        self.as_of = aware(strategy_clock)
        self.context = self.client.at(self.as_of, visibility=self.visibility)
        return self

    def history(self, sid, frequency, query_end, count, fields, adjust_type='none'):
        if self.context is None: raise CapabilityError('CLOCK_NOT_PINNED')
        if adjust_type != 'none': raise CapabilityError('ASHARE_ADJUSTMENTS_UNAVAILABLE')
        mapping = {'total_turnover':'money'}
        requested = [mapping.get(f,f) for f in fields]
        # Query bound and fixed strategy as_of remain independent. A数达 owns all
        # coverage/PIT/quality decisions; do not fill missing data or catch errors.
        frame = self.context.get_price(sid, end_date=query_end,
            frequency='daily' if frequency == '1d' else frequency,
            count=count, fields=requested, fq=None, fill_paused=False)
        return frame.rename(columns={'money':'total_turnover'})

    def require_engine_admission(self):
        raise CapabilityError('ASHARE_DEV2_ENGINE_ADMISSION_MISSING:historical_master,calendar_assets,actions,adjustments,limits')
