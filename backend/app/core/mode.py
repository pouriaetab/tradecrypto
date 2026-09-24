"""Execution mode at runtime.

The mode used to live only in .env, which meant switching between paper and
advisory needed a file edit and a restart. It is now switchable from the UI and
persisted in the database, so it survives a restart and is the same for every
process reading this database.

The safety property is unchanged and is the reason this file exists rather than
a plain variable:

    paper     -> always allowed. Simulated fills, no real orders.
    advisory  -> always allowed. Writes tickets for a human; still no real orders.
    mcp       -> allowed ONLY if .env contains
                 TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK

A UI toggle can therefore never, by itself, put real money at risk. Turning on
live trading still requires editing a file on disk and restarting — a deliberate
speed bump that a mis-click cannot clear.
"""
from __future__ import annotations

import time

from app.config import get_settings
from app.core import db

VALID = ("paper", "advisory", "mcp")
LABELS = {
    "paper": "Paper",
    "advisory": "Advisory",
    "mcp": "Live",
}
DESCRIPTIONS = {
    "paper": "Simulated fills against live quotes, degraded by the measured cost model. No real orders.",
    "advisory": "The engine posts entry and exit tickets for you to execute by hand. No real orders.",
    "mcp": "The engine places real orders in your Robinhood Agentic account.",
}


def _ensure() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ts REAL NOT NULL)""")


def get_mode() -> str:
    """The mode actually in force. Falls back to .env when never overridden."""
    _ensure()
    row = db.query_one("SELECT value FROM app_state WHERE key='execution_mode'")
    m = (row["value"] if row else get_settings().execution_mode).lower()
    if m not in VALID:
        return "paper"
    # A live override cannot survive the confirmation string being removed.
    if m == "mcp" and not live_allowed():
        return "paper"
    return m


def live_allowed() -> bool:
    return get_settings().live_confirm.strip() == "I_ACCEPT_REAL_MONEY_RISK"


def set_mode(mode: str) -> dict:
    mode = (mode or "").strip().lower()
    if mode not in VALID:
        raise ValueError(f"mode must be one of {VALID}")
    if mode == "mcp" and not live_allowed():
        return {
            "changed": False,
            "mode": get_mode(),
            "reason": (
                "Live trading cannot be enabled from the UI. Set "
                "TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK in .env and restart. "
                "The extra step is deliberate: a mis-click should not be able to "
                "start risking real money."
            ),
        }
    _ensure()
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES ('execution_mode', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (mode, time.time()))
    db.log_event("WARNING" if mode == "mcp" else "INFO", "mode",
                 f"execution mode switched to {mode}")
    return {"changed": True, "mode": mode, "reason": DESCRIPTIONS[mode]}


def state() -> dict:
    m = get_mode()
    return {
        "mode": m,
        "label": LABELS[m],
        "description": DESCRIPTIONS[m],
        "live_allowed": live_allowed(),
        "live_active": m == "mcp",
        "options": [
            {"value": v, "label": LABELS[v], "description": DESCRIPTIONS[v],
             "enabled": v != "mcp" or live_allowed(),
             "locked_reason": (None if v != "mcp" or live_allowed()
                               else "requires TC_LIVE_CONFIRM in .env plus a restart")}
            for v in VALID
        ],
    }


# ── account equity ───────────────────────────────────────────────────────────
# TC_ACCOUNT_EQUITY in .env is only a default. The real balance lives here so it
# can be corrected without a restart, and so position sizing uses the amount that
# is actually in the account rather than a number someone typed once.
def get_equity() -> float:
    _ensure()
    row = db.query_one("SELECT value FROM app_state WHERE key='account_equity'")
    if row:
        try:
            return float(row["value"])
        except ValueError:
            pass
    return float(get_settings().account_equity)


def set_equity(amount: float, note: str = "") -> dict:
    if amount <= 0:
        raise ValueError("equity must be positive")
    _ensure()
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES ('account_equity', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (str(float(amount)), time.time()))
    db.log_event("INFO", "account", f"account equity set to ${amount:,.2f} {note}".strip())
    return equity_state()


def equity_state() -> dict:
    row = db.query_one("SELECT value, updated_ts FROM app_state WHERE key='account_equity'")
    return {
        "equity_usd": get_equity(),
        "source": "entered by you" if row else "TC_ACCOUNT_EQUITY default in .env",
        "updated_ts": row["updated_ts"] if row else None,
        "note": ("This app cannot read your Robinhood balance — nothing here is connected "
                 "to your account. This number is what position sizing uses, so it should "
                 "match what you actually funded."),
    }
