"""The token must survive a proxy standing in front of the app.

    "so then option a and b are out of the question"

Option A (same wifi) really was out: the building's network isolates its clients
from each other, and no setting on the Mac changes that. The replacement is a
tunnel — the Mac dials out to Cloudflare, Cloudflare hands back a public https
address — which works through a full-tunnel VPN and needs nothing on the phone.

It also puts a trading dashboard on the public internet, and that changes what
the access guard has to get right. The request chain becomes

    phone -> Cloudflare -> cloudflared (on the Mac) -> vite -> this backend

so by the time a request reaches the backend its socket address is 127.0.0.1.
The loopback exemption — which exists so the desktop is never challenged — would
then wave through the entire internet, and the token would protect nothing.

These tests fail if that exemption ever stops looking at the headers.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import access                     # noqa: E402


class _Req:
    """The smallest thing the guard actually reads."""

    def __init__(self, host="127.0.0.1", path="/api/v1/account/balance",
                 headers=None, query=None, cookies=None, method="GET"):
        self.client = type("C", (), {"host": host})()
        self.url = type("U", (), {"path": path})()
        self.method = method
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = query or {}
        self.cookies = cookies or {}


async def _run(req):
    reached = {"yes": False}

    async def call_next(_):
        reached["yes"] = True
        return "PASSED_THROUGH"

    res = await access.guard(req, call_next)
    return reached["yes"], res


def _status(res):
    return getattr(res, "status_code", 200)


def test_local_desktop_is_never_challenged(monkeypatch):
    monkeypatch.delenv("TC_LAN", raising=False)
    monkeypatch.delenv("TC_TUNNEL", raising=False)
    ok, _ = asyncio.run(_run(_Req()))
    assert ok, "a request from this machine must not need a token"


def test_cloudflare_header_defeats_the_loopback_exemption(monkeypatch):
    """THE ONE THAT MATTERS. Loopback socket, but Cloudflare names the caller."""
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, res = asyncio.run(_run(_Req(
        host="127.0.0.1", headers={"CF-Connecting-IP": "203.0.113.9"})))
    assert not ok, (
        "a request that arrived through the tunnel was treated as local. The "
        "whole internet would have full access to the trading desk.")
    assert _status(res) == 401


def test_forwarded_for_defeats_the_loopback_exemption(monkeypatch):
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, res = asyncio.run(_run(_Req(
        host="127.0.0.1", headers={"X-Forwarded-For": "203.0.113.9, 127.0.0.1"})))
    assert not ok and _status(res) == 401


def test_the_right_token_gets_in(monkeypatch):
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, _ = asyncio.run(_run(_Req(
        host="127.0.0.1",
        headers={"CF-Connecting-IP": "203.0.113.9", "X-TC-Token": access.token()})))
    assert ok


def test_a_wrong_token_does_not(monkeypatch):
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, res = asyncio.run(_run(_Req(
        host="127.0.0.1",
        headers={"CF-Connecting-IP": "203.0.113.9", "X-TC-Token": "not-the-token"})))
    assert not ok and _status(res) == 401


def test_an_empty_token_is_not_accepted(monkeypatch):
    """An empty string compared against an empty stored token would otherwise
    match, which is how a missing secrets file becomes an open door."""
    monkeypatch.setenv("TC_TUNNEL", "1")
    monkeypatch.setattr(access, "token", lambda: "")
    ok, res = asyncio.run(_run(_Req(
        host="127.0.0.1", headers={"CF-Connecting-IP": "203.0.113.9"})))
    assert not ok and _status(res) == 401


def test_nothing_outside_gets_in_when_exposure_is_off(monkeypatch):
    monkeypatch.delenv("TC_LAN", raising=False)
    monkeypatch.delenv("TC_TUNNEL", raising=False)
    ok, res = asyncio.run(_run(_Req(host="192.168.1.50")))
    assert not ok and _status(res) == 403


def test_health_stays_open_so_the_page_can_say_what_is_wrong(monkeypatch):
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, _ = asyncio.run(_run(_Req(
        host="127.0.0.1", path="/health",
        headers={"CF-Connecting-IP": "203.0.113.9"})))
    assert ok


# ── the two refusals must stay distinguishable, and each must be actionable ───
# The app shows a different screen for each: 401 offers a token box, 403 says
# phone access is off on the Mac and offers nothing to type. They were handled
# identically once, and a phone spent a morning pasting a valid token into a
# backend that was never going to look at one.

def _body(res):
    import json
    raw = getattr(res, "body", b"") or b"{}"
    try:
        return json.loads(raw)
    except Exception:
        return {}


def test_a_closed_backend_says_how_to_open_it_not_how_to_get_a_token(monkeypatch):
    monkeypatch.delenv("TC_LAN", raising=False)
    monkeypatch.delenv("TC_TUNNEL", raising=False)
    ok, res = asyncio.run(_run(_Req(headers={"CF-Connecting-IP": "203.0.113.9"})))
    assert not ok and _status(res) == 403
    b = _body(res)
    advice = f"{b.get('error', '')} {b.get('how', '')}".lower()
    assert "localhost" in advice
    assert "tc_lan=1" in advice or "tc_tunnel=1" in advice
    # Sending someone hunting for a token here is the circle we just got out of.
    assert "paste" not in advice


def test_a_missing_token_says_how_to_get_one(monkeypatch):
    monkeypatch.setenv("TC_TUNNEL", "1")
    ok, res = asyncio.run(_run(_Req(headers={"CF-Connecting-IP": "203.0.113.9"})))
    assert not ok and _status(res) == 401
    b = _body(res)
    assert "token" in f"{b.get('error', '')} {b.get('how', '')}".lower()
