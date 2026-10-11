from .calendar_cases import CASES as CALENDAR_CASES
from .carry_cases import CASES as CARRY_CASES
from .dividend_cases import CASES as DIVIDEND_CASES
from .fee_cases import CASES as FEE_CASES
from .lifecycle_cases import CASES as LIFECYCLE_CASES
from .multi_cases import CASES as MULTI_CASES
from .point_cases import CASES as POINT_CASES
from .portfolio_cases import CASES as PORTFOLIO_CASES
from .price_cases import CASES as PRICE_CASES
from .state_cases import CASES as STATE_CASES

CASES = {
    **CALENDAR_CASES,
    **STATE_CASES,
    **POINT_CASES,
    **PRICE_CASES,
    **MULTI_CASES,
    **LIFECYCLE_CASES,
    **FEE_CASES,
    **PORTFOLIO_CASES,
    **CARRY_CASES,
    **DIVIDEND_CASES,
}
