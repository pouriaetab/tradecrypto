"""Crypto news and market events.

An honest framing before anything else: **news is context, not signal.** By the
time a headline reaches an RSS feed, it has been in the market for seconds to
minutes, and the fast money has already traded it. Anyone selling you "news
sentiment alpha" at retail latency is selling you something that was arbitraged
before you read it.

What news is genuinely good for here, and what this module is scoped to:

  1. EXPLAINING a move you already saw. "ARB is up 9%" is more useful with
     "Arbitrum announced X this morning" attached to it.
  2. VETOING a trade. A coin in the middle of an exchange hack, a delisting, or a
     regulatory action is not a coin whose mean-reversion statistics apply. This
     is the defensive use, and it is the one worth acting on.
  3. Post-hoc attribution in the journal — was the loss a model failure or an
     event nobody could have modelled?

Sources are public RSS feeds, parsed with the standard library. No API key, no
vendor, nothing to expire.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from app.core import db

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
]

# Symbol -> the words a headline would actually use.
COIN_ALIASES: dict[str, list[str]] = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"],
    "XRP": ["ripple", "xrp"], "DOGE": ["dogecoin"], "ADA": ["cardano"],
    "AVAX": ["avalanche"], "LINK": ["chainlink"], "LTC": ["litecoin"],
    "BCH": ["bitcoin cash"], "XLM": ["stellar"], "ETC": ["ethereum classic"],
    "UNI": ["uniswap"], "AAVE": ["aave"], "COMP": ["compound"],
    "SHIB": ["shiba inu", "shiba"], "PEPE": ["pepe"], "BONK": ["bonk"],
    "WIF": ["dogwifhat"], "DOT": ["polkadot"], "NEAR": ["near protocol"],
    "APT": ["aptos"], "ATOM": ["cosmos"], "ARB": ["arbitrum"], "OP": ["optimism"],
    "SUI": ["sui"], "SEI": ["sei network", "sei"], "TIA": ["celestia"],
    "INJ": ["injective"], "RENDER": ["render network", "render"],
    "FET": ["fetch.ai", "artificial superintelligence alliance"],
    "GRT": ["the graph"], "SAND": ["sandbox"], "MANA": ["decentraland"],
    "CRV": ["curve finance", "curve dao"], "MKR": ["maker", "makerdao"],
    "LDO": ["lido"], "STX": ["stacks"], "HBAR": ["hedera"], "ALGO": ["algorand"],
    "VET": ["vechain"], "FIL": ["filecoin"], "ICP": ["internet computer"],
    "IMX": ["immutable"], "WLD": ["worldcoin"], "ONDO": ["ondo finance", "ondo"],
    "ENA": ["ethena"], "JUP": ["jupiter"], "POL": ["polygon", "matic"],
    "TRUMP": ["trump coin", "official trump"], "PENGU": ["pudgy penguins"],
    "TON": ["toncoin", "the open network"],
}

# Event categories worth reacting to, most severe first.
EVENT_PATTERNS = [
    ("hack", r"\b(hack|exploit|breach|drain|stolen|rug pull|rugpull)\b", "critical"),
    ("delisting", r"\b(delist|delisting|suspend trading|halt trading)\b", "critical"),
    ("regulation", r"\b(sec |lawsuit|subpoena|indict|sanction|ban |crackdown|regulat)\b", "serious"),
    ("bankruptcy", r"\b(bankrupt|insolven|liquidat|collapse)\b", "critical"),
    ("listing", r"\b(list on|now listed|new listing|debut)\b", "info"),
    ("etf", r"\b(etf|exchange[- ]traded fund)\b", "info"),
    ("upgrade", r"\b(upgrade|hard fork|mainnet|testnet|halving)\b", "info"),
    ("partnership", r"\b(partner|integrat|collaborat)\b", "info"),
    ("unlock", r"\b(token unlock|vesting|cliff)\b", "warning"),
    ("macro", r"\b(fed|fomc|inflation|cpi|rate cut|rate hike|treasury)\b", "info"),
]


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guid TEXT UNIQUE,
        ts REAL NOT NULL,
        fetched_ts REAL NOT NULL,
        source TEXT NOT NULL,
        title TEXT NOT NULL,
        link TEXT,
        summary TEXT,
        symbols TEXT,
        categories TEXT,
        severity TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_news_ts ON news(ts DESC)")


def _parse_date(s: str | None) -> float:
    if not s:
        return time.time()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    return time.time()


def _strip(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:500]


def classify(text: str) -> tuple[list[str], str]:
    low = text.lower()
    cats, sev = [], "info"
    rank = {"info": 0, "warning": 1, "serious": 2, "critical": 3}
    for name, pattern, severity in EVENT_PATTERNS:
        if re.search(pattern, low):
            cats.append(name)
            if rank[severity] > rank[sev]:
                sev = severity
    return cats, sev


def match_symbols(text: str, universe: list[str] | None = None) -> list[str]:
    """Which coins is this headline actually about?

    THE BUG THIS FIXES, because it is worth stating exactly:

        "Robinhood Chain fees collapse 97% even as transactions stay NEAR
         record highs"           -> tagged NEAR, severity CRITICAL

    The matcher lowercased the headline and looked for the ticker as a word, so
    the English word "near" matched the NEAR ticker, and "collapse" pushed it to
    critical. The app then told the operator not to trade NEAR -- a coin he was
    actively trading -- on the strength of a story about Robinhood's chain fees.
    Every NEAR match in the stored history was the English word: "Bitcoin coils
    near $76.5K", "transactions stay near record highs". Not one was the coin.

    Dozens of tickers are ordinary English: NEAR, LINK, SAND, GAS, APE, OP, SUI,
    JUP, ID, MASK, TIME. A stoplist would need maintaining forever and would
    still be wrong for the next listing, so the rule is structural instead:

      * an ALIAS ("near protocol", "chainlink") matches case-insensitively --
        nobody writes those by accident;
      * a BARE TICKER must appear as people actually write tickers: uppercase,
        or with a $ in front. "NEAR" and "$NEAR" count. "near" and "Near" do not.

    A headline shouted entirely in capitals would defeat that, so those fall
    back to aliases only.
    """
    low = text.lower()
    shouty = text.isupper()
    hits = []
    for sym, aliases in COIN_ALIASES.items():
        if universe and sym not in universe:
            continue
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", low):
                hits.append(sym)
                break
        else:
            if len(sym) > 2 and not shouty:
                # Case SENSITIVE, against the original text: this is the whole fix.
                if re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(sym)}\b", text):
                    hits.append(sym)
    return sorted(set(hits))


MATCHER_VERSION = "2"      # bump when match_symbols changes; stored rows get re-tagged


def retag_stored(universe: list[str] | None = None, days: float = 14.0) -> int:
    """Re-run the symbol matcher over stored headlines when the matcher changes.

    Fixing `match_symbols` was not enough on its own: the NEAR false positive
    was already IN the table, and `alerts()` reads the table, so the "Do not
    trade: NEAR" banner outlived the fix by a day. A classifier fix has to
    reach the rows it already classified, or it has fixed nothing visible.
    """
    ensure_schema()
    try:
        from app.core import mode as _mode
        _mode._ensure()          # app_state may not exist yet on a fresh database
        row = db.query_one("SELECT value FROM app_state WHERE key='news_matcher_version'")
        if row and str(row["value"]) == MATCHER_VERSION:
            return 0
    except Exception:
        return 0
    universe = universe or [r["symbol"] for r in db.query("SELECT symbol FROM universe WHERE active=1")]
    rows = db.query("SELECT id, title, summary, symbols FROM news WHERE ts >= ?",
                    (time.time() - days * 86400.0,))
    changed = 0
    for r in rows:
        syms = match_symbols(f"{r['title']} {r['summary'] or ''}", universe)
        new = ",".join(syms)
        if new != (r["symbols"] or ""):
            db.execute("UPDATE news SET symbols=? WHERE id=?", (new, r["id"]))
            changed += 1
    db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES ('news_matcher_version', ?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
               (MATCHER_VERSION, time.time()))
    if changed:
        db.log_event("INFO", "news", f"re-tagged {changed} stored headline(s) with matcher v{MATCHER_VERSION}")
    return changed


def fetch(timeout: float = 12.0) -> dict:
    """Pull every feed and store what is new. Safe to call repeatedly."""
    ensure_schema()
    universe = [r["symbol"] for r in db.query("SELECT symbol FROM universe WHERE active=1")]
    try:
        retag_stored(universe)
    except Exception as exc:          # never let housekeeping stop the fetch
        db.log_event("WARNING", "news", f"retag skipped: {exc}")
    added, errors = 0, []
    per_feed: dict[str, int] = {}
    now = time.time()

    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "tradecrypto/0.1 (personal research)"}) as c:
        for source, url in FEEDS:
            try:
                r = c.get(url)
                if r.status_code >= 400:
                    errors.append(f"{source}: HTTP {r.status_code} — the feed refused the request")
                    continue
                root = ET.fromstring(r.content)
            except ET.ParseError as exc:
                errors.append(f"{source}: not valid XML ({exc}) — the feed URL may have moved")
                continue
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
                continue
            per_feed[source] = 0

            items = root.findall(".//item") or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry")
            for it in items[:40]:
                def g(tag, alt=None):
                    e = it.find(tag)
                    if e is None and alt:
                        e = it.find(alt)
                    return (e.text or "") if e is not None else ""

                title = g("title", "{http://www.w3.org/2005/Atom}title").strip()
                if not title:
                    continue
                link = g("link", "{http://www.w3.org/2005/Atom}link").strip()
                if not link:
                    le = it.find("{http://www.w3.org/2005/Atom}link")
                    link = le.get("href", "") if le is not None else ""
                guid = (g("guid") or link or title)[:400]
                summary = _strip(g("description",
                                   "{http://www.w3.org/2005/Atom}summary"))
                ts = _parse_date(g("pubDate", "{http://www.w3.org/2005/Atom}updated"))

                blob = f"{title} {summary}"
                syms = match_symbols(blob, universe)
                cats, sev = classify(blob)
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO news(guid, ts, fetched_ts, source, title,
                                                      link, summary, symbols, categories, severity)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (guid, ts, now, source, title, link, summary,
                         ",".join(syms), ",".join(cats), sev))
                    added += 1
                    per_feed[source] = per_feed.get(source, 0) + 1
                except Exception as exc:
                    errors.append(f"{source} insert: {exc}")

    db.log_event("INFO", "news", f"fetched feeds: {added} items considered, {len(errors)} errors")
    stored = db.query_one("SELECT COUNT(*) c FROM news")["c"]
    return {
        "considered": added, "errors": errors,
        "per_feed": per_feed,
        "total_stored": stored,
        "sources": [s for s, _ in FEEDS],
        "diagnosis": (
            "All feeds failed — check this machine's network, or the feed URLs have moved."
            if len(errors) >= len(FEEDS) else
            f"{len(per_feed)} of {len(FEEDS)} feeds responded."
        ),
    }


def recent(limit: int = 60, symbol: str | None = None,
           severity: str | None = None, hours: float | None = None) -> dict:
    ensure_schema()
    sql = "SELECT * FROM news WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND symbols LIKE ?"
        params.append(f"%{symbol.upper()}%")
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if hours:
        sql += " AND ts >= ?"
        params.append(time.time() - hours * 3600)
    rows = db.query(sql + " ORDER BY ts DESC LIMIT ?", [*params, limit])
    for r in rows:
        r["symbols"] = [s for s in (r["symbols"] or "").split(",") if s]
        r["categories"] = [s for s in (r["categories"] or "").split(",") if s]
    return {
        "rows": rows,
        "n": len(rows),
        "caveat": (
            "News is CONTEXT, not signal. By the time a headline reaches an RSS feed "
            "it has been in the market for seconds to minutes. Use it to explain a "
            "move you already saw, and to VETO trading a coin in the middle of a hack, "
            "delisting or regulatory action — not to predict the next one."
        ),
    }


def alerts(hours: float = 24.0) -> dict:
    """The defensive use: coins we could trade that are in the middle of something."""
    ensure_schema()
    try:
        retag_stored()
    except Exception:
        pass
    rows = db.query(
        """SELECT * FROM news WHERE ts >= ? AND severity IN ('critical','serious')
           AND symbols != '' ORDER BY ts DESC LIMIT 40""",
        (time.time() - hours * 3600,))
    by_symbol: dict[str, list] = {}
    for r in rows:
        for s in (r["symbols"] or "").split(","):
            if s:
                by_symbol.setdefault(s, []).append(
                    {"title": r["title"], "severity": r["severity"],
                     "categories": r["categories"], "link": r["link"], "ts": r["ts"]})
    return {
        "hours": hours,
        "flagged_symbols": sorted(by_symbol),
        "by_symbol": by_symbol,
        "recommendation": (
            "Statistics fitted on normal trading do not apply to a coin being "
            "delisted or drained. Treat a flagged coin as untradeable until the "
            "event resolves, rather than trusting the model through it."
        ),
    }
