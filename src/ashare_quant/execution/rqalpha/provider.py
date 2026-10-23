"""Provider errors and exchange-time normalization."""
from zoneinfo import ZoneInfo
import pandas as pd
TZ = ZoneInfo("Asia/Shanghai")

class CapabilityError(RuntimeError):
    pass


def aware(dt):
    return pd.Timestamp(dt).to_pydatetime().replace(tzinfo=TZ) if pd.Timestamp(dt).tzinfo is None else pd.Timestamp(dt).to_pydatetime().astimezone(TZ)
