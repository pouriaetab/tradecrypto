"""The Trade Desk: advisory execution, where the engine decides and a human clicks.

This is the mode the operator asked for to start with, and it is the right first
mode for a reason beyond caution: every manual fill he reports back is a real
measurement of Robinhood's spread on that specific coin at that specific size.
Fifty advisory trades produce fifty cost observations, which is exactly what the
per-symbol cost model needs to stop guessing.

So the loop is:

  1. engine decides       -> a ticket appears here with the exact instruction
  2. operator executes    -> in the Robinhood app, by hand
  3. operator reports     -> the real fill price goes back into the system
  4. engine tracks        -> the position is live, with its stop, target and clock
  5. engine says exit     -> a close ticket appears
  6. operator reports     -> the round trip closes, P&L and attribution are computed
                             and the cost model learns from both fills

Nothing is assumed filled. A ticket that is never reported stays pending and
expires; a position is only real once a fill price comes back.
"""
from __future__ import annotations

import json
import time

from app.core import db
from app.execution import cost_model
from app.feedback import loop as feedback

TICKET_TTL_S = 300.0        # a signal older than five minutes is stale, not a trade


def pending_tickets(mode: str = "advisory") -> list[dict]:
    rows = db.query(
        """SELECT * FROM orders WHERE mode=? AND status='pending'
           ORDER BY ts_decided DESC LIMIT 50""", (mode,))
    now = time.time()
    out = []
    for r in rows:
        ticket = json.loads(r["ticket_json"]) if r["ticket_json"] else {}
        age = now - r["ts_decided"]
        out.append({
            **r,
            "ticket": ticket,
            "age_seconds": age,
            "expired": age > TICKET_TTL_S,
            "instruction": ticket.get("action"),
        })
    return out


def has_pending_close(symbol: str, mode: str) -> bool:
    row = db.query_one(
        """SELECT 1 FROM orders WHERE symbol=? AND mode=? AND intent='close'
           AND status='pending' AND ts_decided > ?""",
        (symbol.upper(), mode, time.time() - TICKET_TTL_S))
    return bool(row)


def skip(client_id: str, reason: str = "operator skipped") -> dict:
    db.execute("UPDATE orders SET status='cancelled', reject_reason=? WHERE client_id=?",
               (reason, client_id))
    db.log_event("INFO", "desk", f"ticket {client_id} skipped: {reason}")
    return {"client_id": client_id, "status": "cancelled"}


def expire_stale(mode: str = "advisory") -> int:
    n = 0
    for t in pending_tickets(mode):
        if t["expired"]:
            db.execute("UPDATE orders SET status='cancelled', reject_reason=? WHERE client_id=?",
                       ("expired before execution", t["client_id"]))
            n += 1
    if n:
        db.log_event("INFO", "desk", f"expired {n} stale tickets")
    return n


def report_fill(client_id: str, fill_price: float, qty: float | None = None,
                fee_usd: float = 0.0) -> dict:
    """Record a real, human-executed fill and advance the position state machine."""
    o = db.query_one("SELECT * FROM orders WHERE client_id=?", (client_id,))
    if not o:
        raise KeyError(f"unknown client_id {client_id!r}")
    if o["status"] == "filled":
        return {"client_id": client_id, "already_filled": True,
                "fill_price": o["fill_price"]}

    px = float(fill_price)
    if px <= 0:
        raise ValueError("fill price must be positive")
    q = float(qty) if qty else (o["notional_usd"] / px)

    db.execute(
        "UPDATE orders SET status='filled', fill_price=?, qty=?, ts_filled=?, fee_usd=? WHERE client_id=?",
        (px, q, time.time(), fee_usd, client_id))

    # Every human fill is a genuine measurement of what this venue charges.
    cost = cost_model.record_observation(
        symbol=o["symbol"], side=o["side"], notional_usd=o["notional_usd"], fill_px=px,
        mid_at_submit=o["mid_at_submit"], mid_at_decision=o["mid_at_decision"],
        mode=o["mode"], source="measured")

    result = {"client_id": client_id, "fill_price": px, "qty": q,
              "cost_measured": cost, "intent": o["intent"]}

    if o["intent"] == "open":
        sig = db.query_one("SELECT * FROM signals WHERE id=?", (o["signal_id"],)) if o["signal_id"] else None
        stop_bps = 150.0
        target_bps = 150.0
        hold_s = 1800.0
        if sig and sig["features_json"]:
            f = json.loads(sig["features_json"])
            stop_bps = float(f.get("stop_bps", stop_bps))
            target_bps = float(f.get("target_bps", target_bps))
            hold_s = float(f.get("hold_seconds", hold_s))
        sgn = 1 if o["side"] == "buy" else -1
        db.execute(
            """INSERT OR REPLACE INTO positions(symbol, mode, strategy, qty, avg_px, opened_ts,
                                                stop_px, target_px, max_hold_s)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (o["symbol"], o["mode"], o["strategy"], sgn * q, px, time.time(),
             px * (1 - sgn * stop_bps / 1e4), px * (1 + sgn * target_bps / 1e4), hold_s))
        result["position_opened"] = {
            "symbol": o["symbol"], "qty": sgn * q, "entry": px,
            "stop": px * (1 - sgn * stop_bps / 1e4),
            "target": px * (1 + sgn * target_bps / 1e4),
            "max_hold_minutes": hold_s / 60,
        }
        db.log_event("INFO", "desk", f"position opened by hand: {o['symbol']} @ {px}")

    else:  # close
        pos = db.query_one("SELECT * FROM positions WHERE symbol=? AND mode=? AND strategy=?",
                           (o["symbol"], o["mode"], o["strategy"]))
        if pos:
            sgn = 1 if pos["qty"] > 0 else -1
            qty_abs = abs(pos["qty"])
            gross = sgn * (px - pos["avg_px"]) * qty_abs
            entry_order = db.query_one(
                """SELECT * FROM orders WHERE symbol=? AND mode=? AND intent='open'
                   AND status='filled' ORDER BY ts_filled DESC LIMIT 1""",
                (o["symbol"], o["mode"]))
            entry_mid = entry_order["mid_at_submit"] if entry_order else pos["avg_px"]
            cost_usd = (abs(pos["avg_px"] - (entry_mid or pos["avg_px"]))
                        + abs(px - (o["mid_at_submit"] or px))) * qty_abs + fee_usd
            trade_id = db.execute(
                """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open,
                                      ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd,
                                      predicted_edge_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o["symbol"], pos["strategy"], o["mode"], pos["qty"], pos["avg_px"], px,
                 pos["opened_ts"], time.time(), time.time() - pos["opened_ts"],
                 gross, cost_usd, gross - cost_usd, None))
            db.execute("DELETE FROM positions WHERE symbol=? AND mode=? AND strategy=?",
                       (o["symbol"], o["mode"], pos["strategy"]))
            feedback.attribute_trade(trade_id)
            feedback.update_posterior(pos["strategy"], o["mode"])
            result["trade_closed"] = {
                "trade_id": trade_id, "gross_usd": gross, "cost_usd": cost_usd,
                "net_usd": gross - cost_usd,
                "held_minutes": (time.time() - pos["opened_ts"]) / 60,
            }
            db.log_event("INFO", "desk",
                         f"round trip closed by hand: {o['symbol']} net ${gross - cost_usd:.2f}")

    result["cost_estimate_now"] = cost_model.estimate(o["symbol"]).to_dict()
    return result


def desk_state(mode: str = "advisory") -> dict:
    """Everything the operator needs on one screen to act right now."""
    expire_stale(mode)
    tickets = [t for t in pending_tickets(mode) if not t["expired"]]
    positions = db.query("SELECT * FROM positions WHERE mode=? AND qty != 0", (mode,))
    for p in positions:
        p["held_minutes"] = (time.time() - p["opened_ts"]) / 60
        p["deadline_minutes"] = ((p["opened_ts"] + (p["max_hold_s"] or 0)) - time.time()) / 60
        p["close_ticket_pending"] = has_pending_close(p["symbol"], mode)
    return {
        "mode": mode,
        "open_tickets": tickets,
        "positions": positions,
        "recent_fills": db.query(
            """SELECT * FROM orders WHERE mode=? AND status='filled'
               ORDER BY ts_filled DESC LIMIT 20""", (mode,)),
        "ticket_ttl_seconds": TICKET_TTL_S,
        "how_this_works": (
            "The engine posts a ticket. You place that exact order in the Robinhood app, "
            "then type the price you actually got. That reported price is what teaches the "
            "cost model what this venue really charges -- it is the measurement, not paperwork."
        ),
    }
