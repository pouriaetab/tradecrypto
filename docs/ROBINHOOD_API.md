# Broker API integration — removed before publication

This file is kept, rather than deleted, because several places in the code and
the interface point at it. A dangling reference is a small lie; a page that
says what happened is not.

## What used to be here

Setup instructions for the Robinhood Crypto Trading REST API: where to create
the key pair, where to put the two files, and how the client signed each
request.

## Why it is gone

The published build cannot place an order. `backend/app/execution/rh_api.py`
was deleted and every call site now imports `app/execution/no_broker.py`, a
stub whose functions raise `NotConfigured`. `Settings.live_enabled` returns
`False` unconditionally and `app/core/mode.py` accepts only `paper` and
`advisory`. The endpoints that used to reach the venue still exist and still
answer — with a clean "not configured" error rather than a traceback, which is
the behaviour the failure-mode tests require of every disabled path.

This was a deliberate decision, not an oversight. The project's measured result
is that the strategy loses money after costs; shipping a public repository that
a reader could point at a funded account would be publishing a known-negative
system with a live trigger attached.

## What survived

The finding, which was the only part worth keeping:
`backend/app/execution/venue_fees.py` holds the measured cost model — roughly
0.95% per side taken in the spread, about 1.90% round trip, against a 2–3%
typical daily range. It is pure data. It makes no network call and needs no
credential.

See `DISCLAIMER.md` for the operating posture and `README.md` §10 for what else
is deliberately absent.
