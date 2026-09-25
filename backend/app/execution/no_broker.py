"""The broker that is deliberately not here.

When this project was published, every piece of code that could reach a
brokerage account or place an order was deleted. This module is what the API
layer imports in its place, so the endpoints that used to talk to a broker
answer honestly instead of crashing.

Why a stub rather than deleting the endpoints too: the routes and their tests
document what the system used to do, and an endpoint that replies "this build
has no broker integration" is more informative to someone reading the code than
a 404. It also makes the removal impossible to miss.

Nothing here opens a socket, reads a credential, or signs anything. The fee
schedule — the one genuinely useful part of the old client, and the source of
this project's central finding — lives on in `venue_fees.py`.
"""
from __future__ import annotations

from app.execution.venue_fees import (  # re-exported: pure data, no account
    DEFAULT_HALF_SPREAD_PCT,
    DEFAULT_ROUND_TRIP_PCT,
    FEE_TIERS,
    fee_for_volume,
)

__all__ = [
    "NotConfigured", "RobinhoodCrypto", "measure_spreads", "sync_universe",
    "routing_comparison", "fee_status", "FEE_TIERS", "fee_for_volume",
    "DEFAULT_HALF_SPREAD_PCT", "DEFAULT_ROUND_TRIP_PCT",
]

REASON = (
    "This build has no broker integration. The order-placing and account code "
    "was removed when the project was published, so there is nothing to "
    "configure and no credential that would change this."
)


class NotConfigured(RuntimeError):
    """Raised by anything that used to need an account.

    The name is unchanged from the client it replaces, because every caller
    already handles it as the normal, expected state rather than as a fault.
    """


class RobinhoodCrypto:
    """Exists only so imports resolve. Constructing one is always an error."""

    configured = False

    def __init__(self, *args, **kwargs):
        raise NotConfigured(REASON)


def _unavailable(*args, **kwargs):
    raise NotConfigured(REASON)


measure_spreads = _unavailable
sync_universe = _unavailable
routing_comparison = _unavailable
fee_status = _unavailable
