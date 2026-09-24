"""Automatic paper discovery.

The library holds what has been read and applied. This adds what has just been
published, so the reading list refreshes itself instead of ageing quietly.

Source is the arXiv API — free, no key, stable — restricted to the categories
that actually bear on this project:

    q-fin.TR   trading and market microstructure
    q-fin.ST   statistical finance
    q-fin.CP   computational finance
    q-fin.PM   portfolio management
    stat.ML    machine learning (methods, filtered by keyword)

New arrivals land with status `queued` and a relevance score, never `applied`.
The rule from `library.py` still holds: a paper only becomes `applied` when a
line of code implements it and the entry names the file. Nothing here can
promote itself.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

import httpx

from app.core import db
from app.research import library

ATOM = "{http://www.w3.org/2005/Atom}"
API = "http://export.arxiv.org/api/query"

CATEGORIES = ["q-fin.TR", "q-fin.ST", "q-fin.CP", "q-fin.PM"]

# Terms that make a paper relevant to what is actually being built here.
RELEVANT = {
    "backtest": 3, "overfit": 3, "market microstructure": 3, "order book": 3,
    "execution cost": 3, "market impact": 3, "transaction cost": 3,
    "cryptocurrency": 3, "crypto": 2, "bitcoin": 2,
    "momentum": 2, "mean revers": 2, "breakout": 3, "limit order": 2,
    "meta-label": 3, "triple barrier": 3, "purged": 3, "deflated sharpe": 3,
    "false discovery": 3, "multiple testing": 3, "calibration": 2,
    "regime": 2, "volatility": 1, "liquidity": 2, "slippage": 3,
    "optimal stopping": 2, "kelly": 2, "position sizing": 2,
    "lead-lag": 3, "cross-correlation": 1, "attention": 1,
}


def _score(text: str) -> tuple[int, list[str]]:
    low = text.lower()
    hits, total = [], 0
    for term, w in RELEVANT.items():
        if term in low:
            hits.append(term)
            total += w
    return total, hits


def fetch(per_category: int = 15, min_score: int = 3) -> dict:
    """Pull recent submissions and file the relevant ones as `queued`."""
    library.ensure_schema()
    existing = {r["key"] for r in db.query("SELECT key FROM papers")}
    added, skipped, errors = 0, 0, []

    with httpx.Client(timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": "tradecrypto/0.1 (personal research)"}) as c:
        for cat in CATEGORIES:
            try:
                r = c.get(API, params={
                    "search_query": f"cat:{cat}",
                    "sortBy": "submittedDate", "sortOrder": "descending",
                    "max_results": per_category})
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as exc:
                errors.append(f"{cat}: {type(exc).__name__}: {exc}")
                continue

            for e in root.findall(f"{ATOM}entry"):
                title = (e.findtext(f"{ATOM}title") or "").strip().replace("\n", " ")
                summary = (e.findtext(f"{ATOM}summary") or "").strip().replace("\n", " ")
                url = (e.findtext(f"{ATOM}id") or "").strip()
                pub = (e.findtext(f"{ATOM}published") or "")[:10]
                authors = ", ".join(
                    (a.findtext(f"{ATOM}name") or "") for a in e.findall(f"{ATOM}author"))[:180]
                if not title or not url:
                    continue
                key = "arxiv:" + url.rsplit("/", 1)[-1]
                if key in existing:
                    skipped += 1
                    continue
                score, hits = _score(f"{title} {summary}")
                if score < min_score:
                    skipped += 1
                    continue
                library.add({
                    "key": key, "title": title, "authors": authors or "arXiv",
                    "year": int(pub[:4]) if pub[:4].isdigit() else time.gmtime().tm_year,
                    "venue": f"arXiv {cat}", "url": url,
                    "one_line": re.sub(r"\s+", " ", summary)[:240] + "…",
                    "used_for": (f"Not yet used. Surfaced automatically because it mentions: "
                                 f"{', '.join(hits[:5])}."),
                    "tags": ["auto", cat.split(".")[-1].lower()],
                    "status": library.QUEUED,
                    "relevance_score": score,
                })
                existing.add(key)
                added += 1

    db.log_event("INFO", "papers", f"paper feed: {added} added, {skipped} skipped, {len(errors)} errors")
    return {"added": added, "skipped": skipped, "errors": errors,
            "sources": len(CATEGORIES),
            "note": ("New arrivals are always 'queued'. A paper only becomes 'applied' when "
                     "a line of code implements it and the entry names the file.")}
