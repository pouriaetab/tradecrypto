"""Brokers. Three of them, deliberately.

paper     -- no real orders. Fills are simulated against the LIVE quote using a
             random draw from the measured cost model, so paper P&L is degraded
             by the same friction real orders face.

advisory  -- the engine writes a signed order ticket and stops. A human, or a
             Claude session that holds the Robinhood MCP connection, executes it
             and reports the fill back. This is the honest bridge while the
             headless OAuth story is unproven: the strategy runs unattended, the
             execution stays supervised.

mcp       -- direct orders through Robinhood's agentic MCP endpoint. Requires a
             one-time desktop OAuth, an Agentic account, and TC_LIVE_CONFIRM.
             It refuses to do anything otherwise, loudly.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from app.config import get_settings
from app.core import db, journal, liveness, vault
from app.execution import cost_model


@dataclass
class OrderRequest:
    symbol: str
    side: str                # buy | sell
    notional_usd: float
    strategy: str
    intent: str = "open"     # open | close
    signal_id: int | None = None
    mid_at_decision: float | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Fill:
    client_id: str
    symbol: str
    side: str
    qty: float
    price: float
    ts: float
    status: str              # filled | pending | rejected
    broker_order_id: str | None = None
    reject_reason: str | None = None


class Broker(Protocol):
    mode: str

    def place(self, req: OrderRequest, quote_mid: float) -> Fill: ...
    def probe(self) -> dict: ...


def _persist_order(req: OrderRequest, client_id: str, mode: str, qty: float,
                   mid_decision: float | None, mid_submit: float | None,
                   status: str, fill_px: float | None, broker_order_id: str | None,
                   reject_reason: str | None, ticket: dict | None) -> int:
    order_id = db.execute(
        """INSERT INTO orders(client_id, signal_id, strategy, symbol, side, intent, qty,
                              notional_usd, mode, status, ts_decided, ts_submitted, ts_filled,
                              mid_at_decision, mid_at_submit, fill_price, broker_order_id,
                              reject_reason, ticket_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (client_id, req.signal_id, req.strategy, req.symbol.upper(), req.side, req.intent,
         qty, req.notional_usd, mode, status, time.time(),
         time.time() if status != "rejected" else None,
         time.time() if status == "filled" else None,
         mid_decision, mid_submit, fill_px, broker_order_id, reject_reason,
         json.dumps(ticket) if ticket else None),
    )
    liveness.fired("order_placed", f"{req.symbol.upper()} {req.side} {status}")
    journal.append("order", {
        "order_id": order_id, "client_id": client_id, "signal_id": req.signal_id,
        "strategy": req.strategy, "symbol": req.symbol.upper(), "side": req.side,
        "intent": req.intent, "qty": qty, "notional_usd": req.notional_usd,
        "mode": mode, "status": status, "fill_px": fill_px,
        "reject_reason": reject_reason,
    })
    # And to the vault, which is not in this project folder, not a database, and
    # not read by anything here. Three copies of every order now: the database,
    # the journal beside it, and one outside the whole system. This call cannot
    # raise and cannot block -- if it fails, vault.reconcile() picks it up.
    vault.record("order", f"order:{order_id}", {
        "order_id": order_id, "client_id": client_id, "signal_id": req.signal_id,
        "strategy": req.strategy, "symbol": req.symbol.upper(), "side": req.side,
        "intent": req.intent, "qty": qty, "notional_usd": req.notional_usd,
        "mode": mode, "status": status, "fill_px": fill_px,
        "reject_reason": reject_reason,
    })
    return order_id


# ──────────────────────────────────────────────────────────────────────────────
class PaperBroker:
    mode = "paper"

    def __init__(self, seed: int | None = None):
        self._rng = np.random.default_rng(seed)

    def probe(self) -> dict:
        return {"mode": self.mode, "ready": True,
                "note": "simulated fills against live quotes, degraded by the measured cost model"}

    def place(self, req: OrderRequest, quote_mid: float) -> Fill:
        cid = f"paper-{uuid.uuid4().hex[:12]}"
        if quote_mid <= 0:
            _persist_order(req, cid, self.mode, 0, req.mid_at_decision, quote_mid,
                           "rejected", None, None, "no valid mid", None)
            return Fill(cid, req.symbol, req.side, 0, 0, time.time(), "rejected",
                        reject_reason="no valid mid")

        fill_px, cost_bps = cost_model.simulate_fill(quote_mid, req.side, req.symbol, self._rng)
        qty = req.notional_usd / fill_px
        _persist_order(req, cid, self.mode, qty, req.mid_at_decision, quote_mid,
                       "filled", fill_px, None, None, {"simulated_cost_bps": cost_bps})
        cost_model.record_observation(
            symbol=req.symbol, side=req.side, notional_usd=req.notional_usd,
            fill_px=fill_px, mid_at_submit=quote_mid, mid_at_decision=req.mid_at_decision,
            mode=self.mode, source="prior",   # simulated: never treated as measurement
        )
        return Fill(cid, req.symbol, req.side, qty, fill_px, time.time(), "filled")


# ──────────────────────────────────────────────────────────────────────────────
class AdvisoryBroker:
    """Writes a ticket, waits for a human-reported fill. Nothing is assumed filled."""

    mode = "advisory"

    def probe(self) -> dict:
        pending = db.query_one("SELECT COUNT(*) c FROM orders WHERE mode='advisory' AND status='pending'")
        return {"mode": self.mode, "ready": True, "pending_tickets": pending["c"] if pending else 0,
                "note": "engine proposes, a human (or a Claude session with the RH MCP) disposes"}

    def place(self, req: OrderRequest, quote_mid: float) -> Fill:
        cid = f"adv-{uuid.uuid4().hex[:12]}"
        ticket = {
            "client_id": cid,
            "action": f"{req.side.upper()} ${req.notional_usd:.2f} of {req.symbol.upper()}",
            "reference_mid": quote_mid,
            "max_acceptable_price": quote_mid * (1.004 if req.side == "buy" else 1.0),
            "min_acceptable_price": quote_mid * (1.0 if req.side == "buy" else 0.996),
            "strategy": req.strategy,
            "expires_at": time.time() + 120,
            "instruction_for_executor": (
                "Place this as a market order on the Robinhood Agentic account, then "
                "report the actual fill price back via POST /api/v1/orders/{client_id}/fill. "
                "If more than 2 minutes have passed, do not place it -- the signal is stale."
            ),
        }
        _persist_order(req, cid, self.mode, 0, req.mid_at_decision, quote_mid,
                       "pending", None, None, None, ticket)
        db.log_event("INFO", "execution", f"advisory ticket {cid}: {ticket['action']}", ticket)
        return Fill(cid, req.symbol, req.side, 0, 0, time.time(), "pending")


# ──────────────────────────────────────────────────────────────────────────────
class RobinhoodMCPBroker:
    """Direct execution through Robinhood's agentic MCP.

    Reality check, so nobody is surprised later: this endpoint is an OAuth-gated
    remote MCP server designed to be driven by an interactive LLM client. It is
    not a keyed REST API. Consequences:

      * A one-time browser OAuth on a desktop is required, and the resulting
        token has to be persisted for unattended use.
      * Rate limits and token lifetime are undocumented publicly.
      * Latency is a network round trip plus tool mediation -- expect seconds,
        not milliseconds. Any strategy that needs sub-second execution is not
        viable here, and the backtester's one-bar delay reflects that.

    Until a token exists, every method raises with instructions instead of
    pretending to work.
    """

    mode = "mcp"

    def __init__(self):
        self.s = get_settings()
        self._session = None

    def _token_path(self):
        from pathlib import Path
        p = Path(self.s.rh_token_path)
        return p if p.is_absolute() else (self.s.db_file.parent.parent / p).resolve()

    def probe(self) -> dict:
        tok = self._token_path()
        try:
            import mcp  # noqa: F401
            sdk = True
        except ImportError:
            sdk = False
        return {
            "mode": self.mode,
            "ready": bool(sdk and tok.exists() and self.s.live_enabled),
            "mcp_sdk_installed": sdk,
            "token_present": tok.exists(),
            "token_path": str(tok),
            "live_confirm_set": self.s.live_enabled,
            "endpoint": self.s.rh_mcp_url,
            "setup_steps": [
                "1. On a desktop, open the Robinhood app/site and enable Agentic Trading; "
                "it creates a separate Agentic account. Fund it with ONLY what you accept losing.",
                "2. Confirm crypto is permitted for agents in your state (it is restricted in some, incl. New York).",
                f"3. Connect the MCP once interactively: claude mcp add --transport http robinhood-trading {self.s.rh_mcp_url}",
                "4. Complete the browser OAuth. Never paste your Robinhood password into any tool.",
                f"5. Persist the resulting token to {tok} for unattended use.",
                "6. Set TC_EXECUTION_MODE=mcp and TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK in .env.",
                "Until all of the above is true, this broker refuses to place orders.",
            ],
        }

    def place(self, req: OrderRequest, quote_mid: float) -> Fill:
        p = self.probe()
        if not p["ready"]:
            cid = f"mcp-blocked-{uuid.uuid4().hex[:8]}"
            reason = "Robinhood MCP not ready: " + ", ".join(
                k for k in ("mcp_sdk_installed", "token_present", "live_confirm_set") if not p[k]
            )
            _persist_order(req, cid, self.mode, 0, req.mid_at_decision, quote_mid,
                           "rejected", None, None, reason, p)
            db.log_event("ERROR", "execution", reason, p)
            return Fill(cid, req.symbol, req.side, 0, 0, time.time(), "rejected", reject_reason=reason)
        raise NotImplementedError(
            "Live MCP order placement is intentionally not wired up until you have "
            "completed the OAuth setup and reviewed the tool names Robinhood's MCP "
            "actually exposes. Use TC_EXECUTION_MODE=advisory in the meantime -- it "
            "runs the full strategy and hands you an executable ticket."
        )


def get_broker(mode: str | None = None) -> Broker:
    from app.core import mode as mode_mod
    m = (mode or mode_mod.get_mode()).lower()
    return {"paper": PaperBroker, "advisory": AdvisoryBroker, "mcp": RobinhoodMCPBroker}[m]()
