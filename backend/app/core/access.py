"""Who is allowed to talk to this backend when it is not just this machine.

WHY THIS EXISTS
---------------
The operator asked to use the app from his phone. That means binding the server
to the local network instead of 127.0.0.1, and the moment that happens the
threat model changes completely. Until now the only thing that could reach this
process was something already running on his Mac. On a LAN, anything on the
network can reach it -- and this process holds Robinhood API credentials, can
switch itself to live mode and can place orders.

So LAN access is opt-in and token-gated. The rules:

  * Requests from loopback are never challenged. Nothing changes for the desktop.
  * Any other source must present the token, as `X-TC-Token` or `?token=`.
  * The token is generated once, stored in secrets/ with 0600 permissions, and
    printed at startup so it can be typed into the phone once.
  * If `TC_LAN` is off, non-loopback requests are refused outright rather than
    token-checked -- a server that was never meant to be reachable should say so
    rather than invite guessing.

This is not a substitute for a real auth system and is not pretending to be one.
It is the minimum that makes "reachable from my phone on my own wifi" different
from "reachable by anything on my own wifi", and it is honest about which of
those two it achieves.
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
HEADER = "x-tc-token"
# Paths the phone must be able to reach before it has a token, so the browser can
# load the page and be told what to do. None of them expose data or take action.
OPEN_PATHS = {"/health", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}


def _token_path() -> Path:
    from app.config import get_settings
    try:
        base = Path(get_settings().rh_credentials_file).parent
    except Exception:
        base = Path("secrets")
    base.mkdir(parents=True, exist_ok=True)
    return base / "lan_token.txt"


def token() -> str:
    """The shared token, created on first use and never regenerated silently."""
    p = _token_path()
    if p.exists():
        t = p.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(18)
    p.write_text(t)
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)      # 0600: owner only
    except OSError:
        pass
    return t


def rotate() -> str:
    p = _token_path()
    if p.exists():
        p.unlink()
    return token()


def lan_enabled() -> bool:
    return os.environ.get("TC_LAN", "").strip().lower() in {"1", "true", "yes", "on"}


def tunnel_enabled() -> bool:
    return os.environ.get("TC_TUNNEL", "").strip().lower() in {"1", "true", "yes", "on"}


def exposed() -> bool:
    """Is this process reachable by anything other than this machine?"""
    return lan_enabled() or tunnel_enabled()


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _forwarded_client(request: Request) -> str | None:
    """The real caller, when a proxy is in front of us.

    THIS IS THE FUNCTION THAT STOPS A TUNNEL FROM BEING AN OPEN DOOR.

    Through a Cloudflare tunnel the chain is

        phone -> Cloudflare -> cloudflared (on this Mac) -> vite -> this backend

    so by the time a request arrives here its socket address is 127.0.0.1. The
    loopback exemption above would then wave through the entire internet, and the
    token would protect nothing at all. The original caller only survives in
    headers, so the headers are what decide.

    `CF-Connecting-IP` is set by Cloudflare itself and is definitive: if it is
    present, the request came from outside. `X-Forwarded-For` is the general
    case; its first entry is the original client.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first and first not in LOOPBACK:
            return first
    return None


async def guard(request: Request, call_next):
    host = _client_host(request)
    path = request.url.path
    forwarded = _forwarded_client(request)

    if path in OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    # Loopback is trusted ONLY when nothing in the headers says otherwise. A
    # request that arrived through a proxy has a loopback socket address and a
    # header naming the real caller; the header wins.
    if host in LOOPBACK and forwarded is None:
        return await call_next(request)

    if not exposed():
        return JSONResponse(
            status_code=403,
            content={"error": "this backend is bound to localhost only",
                     "how": "start it with TC_LAN=1 (same wifi) or TC_TUNNEL=1 "
                            "(from anywhere) and it will print a token to use"})

    supplied = (request.headers.get(HEADER)
                or request.query_params.get("token")
                or request.cookies.get("tc_token")
                or "")
    if supplied and secrets.compare_digest(supplied, token()):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"error": "token required",
                 "from": forwarded or host,
                 "how": "open the app once with ?token=… — it is printed in the "
                        "terminal that started the server, and the page stores it"})
