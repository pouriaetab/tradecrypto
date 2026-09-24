"""The Token Ledger — which features spend model tokens, and on what.

Why this exists before there is anything to spend
--------------------------------------------------
Right now this system calls no language model at all. That is easy to say and
hard to keep true: one convenient import in six months and it stops being true
without anyone noticing. So this page is built the only way it can stay honest —
the declarations below are CHECKED against the source on every request. If the
scan finds a model SDK in a feature that declares it uses none, the page shows a
discrepancy rather than the declaration.

A ledger that is hand-maintained is a ledger that is wrong.

The one feature that genuinely needs a model
---------------------------------------------
Robinhood's agentic trading is an MCP server. There is no REST endpoint to place
a crypto order; an LLM agent authenticates and calls the tools. So order
placement cannot be made zero-token while the broker is Robinhood.

But the MODEL is not fixed. Any MCP-capable model can drive it, so the provider
is switchable — a cheap or free model can place orders just as well as an
expensive one, because the hard thinking (what to trade, how much, when) happens
in numpy long before the order is written. The agent is a typist, not a trader.

Everything else on this page is optional. Where a model could help, the current
non-model implementation is named, so the choice is visible rather than implied.
"""
from __future__ import annotations

import ast
import io
import re
from pathlib import Path

from app.core import db

APP = Path(__file__).resolve().parents[1]
BACKEND = APP.parent

# Providers the operator can pick between. "none" means the feature runs on
# rules or arithmetic and spends nothing.
PROVIDERS = [
    {"key": "none", "label": "No model (rules / arithmetic)", "cost": "free",
     "note": "Deterministic, reproducible, auditable. The default everywhere it is possible."},
    {"key": "anthropic", "label": "Claude (your subscription)", "cost": "your plan's tokens",
     "note": "Whatever plan you are already on."},
    {"key": "gemini_free", "label": "Gemini (free tier)", "cost": "free within quota",
     "note": "Rate-limited, but adequate for writing a tool call or summarising a headline."},
    {"key": "openai", "label": "OpenAI", "cost": "pay per token", "note": ""},
    {"key": "local", "label": "Local model (Ollama etc.)", "cost": "free, your electricity",
     "note": "No data leaves the machine. Slower, and MCP support varies."},
]

# What each feature is, and what it declares about model use. `needs_model` means
# the feature CANNOT work without one; `could_use_model` means it currently runs
# on rules and a model is an optional upgrade.
FEATURES: list[dict] = [
    {
        "key": "order_placement",
        "title": "Placing and cancelling orders",
        "what": "Talks to Robinhood's agentic MCP server to submit a decision that "
                "numpy already made.",
        "needs_model": True,
        "could_use_model": False,
        "default_provider": "gemini_free",
        "modules": ["app/execution/broker.py", "app/execution/desk.py"],
        "why": "Robinhood exposes no REST endpoint for crypto orders. An MCP agent "
               "authenticates and calls the tools, so this cannot be zero-token "
               "while the broker is Robinhood. The model only formats a decision "
               "that was already made, so the cheapest capable one is the right one.",
        "status_when_off": "Paper mode. Orders are simulated locally and no model is called.",
    },
    {
        "key": "news_sentiment",
        "title": "News reading and coin flagging",
        "what": "Pulls RSS headlines and flags coins caught in a hack, delisting or "
                "regulatory event.",
        "needs_model": False,
        "could_use_model": True,
        "default_provider": "none",
        "modules": ["app/data/news.py"],
        "why": "Today this is keyword matching over the feed's own text — free, "
               "instant, and it never invents a story that was not published. A "
               "model would read nuance better and would also occasionally be "
               "confidently wrong about whether your money is at risk.",
        "status_when_off": "Keyword rules over headline and description text.",
    },
    {
        "key": "paper_summaries",
        "title": "Research paper one-liners",
        "what": "The scrollable library of papers, each with a line on what it is.",
        "needs_model": False,
        "could_use_model": True,
        "default_provider": "none",
        "modules": ["app/research/papers_feed.py", "app/research/library.py"],
        "why": "Today the line is the paper's own abstract, trimmed. A model would "
               "write a better line and would occasionally describe a method the "
               "paper does not contain. For a reading list that is a fair trade; "
               "for anything the strategies depend on it is not.",
        "status_when_off": "First 240 characters of the arXiv abstract.",
    },
    {
        "key": "strategy_signals",
        "title": "Strategy signals and sizing",
        "what": "The four strategies, their expected edge, and fractional Kelly sizing.",
        "needs_model": False, "could_use_model": False, "default_provider": "none",
        "modules": ["app/strategy/", "app/execution/engine.py", "app/execution/budget.py"],
        "why": "Deliberately never a model. Every number here must be reproducible "
               "from the same inputs forever, and must be explainable in the Model "
               "Lab. A model that returns a different answer on Tuesday cannot be "
               "backtested, and a backtest is the only evidence any of this works.",
        "status_when_off": "numpy and scipy.",
    },
    {
        "key": "breakout_veto",
        "title": "False-breakout veto",
        "what": "The logistic model that decides whether a level break is real.",
        "needs_model": False, "could_use_model": False, "default_provider": "none",
        "modules": ["app/research/breakout.py"],
        "why": "Hand-written IRLS logistic regression — not even scikit-learn, so "
               "every coefficient is inspectable and the fit is auditable line by "
               "line. A language model here would be unbacktestable and unexplainable.",
        "status_when_off": "IRLS logistic regression, 21 features, purged walk-forward.",
    },
    {
        "key": "cost_and_risk",
        "title": "Cost model, hurdles and risk guards",
        "what": "Robinhood's spread, the round-trip break-even, position limits, kill switch.",
        "needs_model": False, "could_use_model": False, "default_provider": "none",
        "modules": ["app/execution/rh_spread.py", "app/execution/cost_model.py",
                    "app/execution/symbol_cost.py", "app/risk/guards.py"],
        "why": "Arithmetic on published numbers. There is nothing here to be clever "
               "about, and a safety limit that could be talked out of itself is not "
               "a safety limit.",
        "status_when_off": "Arithmetic.",
    },
    {
        "key": "data_and_research",
        "title": "Market data, universe selection, backtesting",
        "what": "Candle collection, which coins to track, walk-forward and acceptance gates.",
        "needs_model": False, "could_use_model": False, "default_provider": "none",
        "modules": ["app/data/", "app/research/model_lab.py", "app/research/backtest.py",
                    "app/research/stats.py"],
        "why": "Public APIs and statistics. Nothing to gain from a model.",
        "status_when_off": "httpx to Coinbase and Kraken, numpy and scipy.",
    },
]

# What the audit looks for.
#
# The first version of this grepped the raw source and immediately reported a
# contradiction in the research library — because that file lists the PAPER
# "Are Transformers Effective for Time Series Forecasting?". A string in a
# reading list is not an import; matching text was the wrong tool for the job.
#
# So imports and calls are now found by parsing the syntax tree, where an import
# is an import and a title is a string. Only hostnames are still read out of
# string literals, which is exactly where a hostname belongs.
_SDK_MODULES = {
    "anthropic", "openai", "cohere", "mistralai", "langchain", "langchain_core",
    "llama_index", "litellm", "ollama", "transformers", "google.generativeai",
    "google_generativeai", "vertexai", "replicate", "groq",
}
_CALL_NAMES = {"generate_content", "create_message"}
_CALL_PATHS = {("messages", "create"), ("completions", "create"),
               ("chat", "completions", "create")}
_HOST = re.compile(r"https?://([a-zA-Z0-9.\-]+)")
_MODEL_HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com",
                "api.cohere.ai", "api.mistral.ai", "openrouter.ai", "api.replicate.com")


def _root(name: str) -> str:
    return (name or "").split(".")[0]


def _attr_path(node):
    """Dotted path of an attribute chain, e.g. client.messages.create."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return tuple(reversed(parts))


def _scan_source(src: str, rel: str):
    """(sdk imports, model calls, hostnames) found in one file."""
    sdk, calls = [], []
    hosts = {m.group(1) for m in _HOST.finditer(src)}
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"{rel}: unparseable ({exc.msg})"], [], hosts
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                if _root(al.name) in _SDK_MODULES or al.name in _SDK_MODULES:
                    sdk.append(f"{rel}:{n.lineno} import {al.name}")
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if _root(mod) in _SDK_MODULES or mod in _SDK_MODULES:
                sdk.append(f"{rel}:{n.lineno} from {mod} import ...")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            path = _attr_path(n.func)
            if n.func.attr in _CALL_NAMES or any(
                    len(path) >= len(q) and tuple(path[-len(q):]) == q for q in _CALL_PATHS):
                calls.append(f"{rel}:{n.lineno} {'.'.join(path)}()")
    return sdk, calls, hosts


def _files_for(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        p = BACKEND / pat
        if p.is_dir():
            out.extend(sorted(p.rglob("*.py")))
        elif p.exists():
            out.append(p)
    return out


def audit_feature(feature: dict) -> dict:
    """Read the actual source. Evidence beats declaration."""
    sdk_hits, call_hits = [], []
    hosts: set = set()
    scanned = 0
    for path in _files_for(feature["modules"]):
        try:
            src = io.open(path, encoding="utf-8").read()
        except OSError:
            continue
        scanned += 1
        a, b, h = _scan_source(src, str(path.relative_to(BACKEND)))
        sdk_hits += a
        call_hits += b
        hosts |= h
    model_hosts = sorted(h for h in hosts if h in _MODEL_HOSTS)
    found = bool(sdk_hits or call_hits or model_hosts)
    declares_none = not feature["needs_model"]
    return {
        "files_scanned": scanned,
        "sdk_imports": sdk_hits[:10],
        "model_calls": call_hits[:10],
        "model_hosts": model_hosts,
        "other_hosts": sorted(h for h in hosts if h not in _MODEL_HOSTS)[:12],
        "evidence_of_model_use": found,
        "discrepancy": bool(found and declares_none),
        "method": ("imports and calls are found by parsing the syntax tree, not by "
                   "matching text: a paper titled 'Are Transformers Effective for "
                   "Time Series Forecasting?' in the reading list is a string, not "
                   "an import, and the first version of this scan was fooled by it"),
        "verdict": ("SOURCE CONTRADICTS THE DECLARATION — a model SDK or endpoint "
                    "appears in a feature that claims to use none"
                    if (found and declares_none) else
                    "source agrees with the declaration"),
    }


def _provider_of(key: str, default: str) -> str:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (f"provider:{key}",))
    return row["value"] if row and row["value"] else default


def set_provider(feature_key: str, provider: str) -> dict:
    keys = {f["key"] for f in FEATURES}
    if feature_key not in keys:
        return {"error": f"unknown feature {feature_key!r}"}
    if provider not in {p["key"] for p in PROVIDERS}:
        return {"error": f"unknown provider {provider!r}"}
    feat = next(f for f in FEATURES if f["key"] == feature_key)
    if provider == "none" and feat["needs_model"]:
        return {"error": (f"{feat['title']} cannot run without a model — Robinhood "
                          f"has no REST endpoint for crypto orders. Pick a cheaper "
                          f"model instead, or stay in paper mode where this feature "
                          f"is never called.")}
    if provider != "none" and not (feat["needs_model"] or feat["could_use_model"]):
        return {"error": (f"{feat['title']} is deliberately model-free: {feat['why']}")}
    db.execute("INSERT INTO app_state(key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (f"provider:{feature_key}", provider))
    db.log_event("INFO", "tokens", f"{feature_key} provider set to {provider}")
    return {"feature": feature_key, "provider": provider}


def ledger() -> dict:
    from app.core import mode
    live = False
    try:
        live = mode.get_mode() == "mcp"
    except Exception:
        pass

    rows = []
    for f in FEATURES:
        provider = _provider_of(f["key"], f["default_provider"])
        audit = audit_feature(f)
        active = f["needs_model"] and live
        rows.append({
            **{k: v for k, v in f.items()},
            "provider": provider,
            "spending_now": bool(active and provider != "none"),
            "switchable": bool(f["needs_model"] or f["could_use_model"]),
            "audit": audit,
        })

    discrepancies = [r["key"] for r in rows if r["audit"]["discrepancy"]]
    spending = [r["key"] for r in rows if r["spending_now"]]
    return {
        "features": rows,
        "providers": PROVIDERS,
        "mode": "live" if live else "paper",
        "spending_now": spending,
        "discrepancies": discrepancies,
        "headline": (
            "No feature is spending tokens. The bot runs entirely on numpy, scipy "
            "and public market data." if not spending else
            f"{len(spending)} feature(s) can spend tokens right now: {', '.join(spending)}."),
        "audit_note": (
            "Every row below was checked against the source when this page loaded. "
            "The declaration is not trusted: the scan looks for model SDK imports, "
            "model API calls and model hostnames in each feature's own files. "
            + ("No discrepancies." if not discrepancies else
               f"DISCREPANCY in: {', '.join(discrepancies)}.")),
    }
