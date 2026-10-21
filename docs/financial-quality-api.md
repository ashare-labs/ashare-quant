# Release-bound financial queries

The product API exposes `POST /api/v1/product/financial/point` and `GET /api/v1/product/financial/coverage`. Both use the selected common release, installed AD content pin, configuration snapshot and existing same-origin protections. They do not submit a job or change an account.

A point request contains security, field, period, source, timezone-aware as_of and mode. The default is official_daily; historical_pit and source_research are explicit alternatives. To query native fields supply both statement and title. Unit evidence does not grant historical availability. The response includes the release receipt, exact field evidence, value or reason for unavailability.

Coverage accepts optional security and day. The frozen correction ledger distinguishes obsolete trading codes, reviewed same-instrument source aliases, explicit suspended days, delisting and unresolved omissions. No suspension is turned into a zero price or an unlimited-trading claim.

AD 0.10.0.dev17 and AQ 0.12.0.dev19 supply these readers. The account bridge retains its separate runtime. Runtime rebuild inputs and rollback configurations are kept in the configured local deployment; no new persistent service is required.
