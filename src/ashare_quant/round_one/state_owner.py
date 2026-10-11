"""Empty-account sessions with separate AD public calendar facts; no action inference."""

from .multi_owner import MultiOwner


class StateOwner(MultiOwner):
    def bootstrap(self, case):
        if case != self.case or case.case_id != "B35" or case.quantity or case.buy_orders:
            raise RuntimeError("STATE_ONLY_EMPTY_ACCOUNT_REQUIRED")
        days = [str(case.day), case.end_day]
        observed = []
        for day in days:
            value = self.call(dict(operation="bootstrap", security=case.security, day=day))
            if (
                value["session_date"] != day
                or not value["source_observation_clocks"]
                or value["conditional_model"]["session_dates"] != [day]
            ):
                raise RuntimeError("STATE_SOURCE_SESSION_LABEL_UNAVAILABLE")
            observed.append(value)
        value = dict(
            security=case.security,
            day=str(case.day),
            calendar=days,
            calendar_kind="observed_sessions_with_AD_bounded_adjacency",
            engine_state_scope="empty_account_no_orders_or_positions",
            owner_bootstraps=observed,
            previous_session=None,
            next_session=None,
            corporate_action_coverage=None,
            overnight_eligibility=None,
        )

        from .calendar_facts import attach_calendar

        return attach_calendar(value, case.security, days)

    def read(self, *args, **kwargs):
        raise RuntimeError("STATE_ONLY_MARKET_OR_LIFECYCLE_READ_NOT_REQUIRED_OR_ADMITTED")

    def descriptor(self):
        value = super().descriptor()
        value["execution_scope"] = (
            "B35 empty account callbacks; AD public calendar adjacency; no price/action inference"
        )
        return value
