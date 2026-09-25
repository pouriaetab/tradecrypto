# Venue universe sync — removed before publication

Kept as a marker for the references that point here. See
`docs/ROBINHOOD_API.md` for the full account of what was removed and why.

## What used to be here

A helper that asked the broker which crypto symbols were actually tradeable,
and posted the answer back to `/api/v1/universe/sync`, so the instrument
universe matched the venue rather than a hand-kept list.

## What happens now

The universe is seeded from Coinbase at startup, unconditionally and without
credentials — about 50 instruments. That path used to be conditional, which
produced the worst bug in this project's history: with no broker configured the
seed never ran, the universe stayed empty, and the application came up healthy
and did nothing at all, forever. The failure was logged at WARNING and
swallowed. `backend/tests/test_silent_failures.py` exists because of it.

Coins the broker would not have traded are therefore included. That is visible
in the interface rather than hidden, and it is listed in `README.md` §7 as a
threat to validity: the backtest universe is broader than any real venue's.
