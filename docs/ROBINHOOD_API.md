# Connecting the Robinhood Crypto API

Spec read from https://docs.robinhood.com/crypto/trading/ on 2026-09-07.

This replaces the MCP agent entirely. No language model is involved, so the bot's
token cost is zero.

## 1. Generate an Ed25519 key pair

```bash
backend/.venv/bin/pip install pynacl
backend/.venv/bin/python - <<'PY'
import base64, nacl.signing
k = nacl.signing.SigningKey.generate()
print("PRIVATE (keep secret):", base64.b64encode(k.encode()).decode())
print("PUBLIC  (give to RH) :", base64.b64encode(k.verify_key.encode()).decode())
PY
```

## 2. Create the credential at Robinhood

Web **classic** → crypto account settings → **Add key**. Paste the PUBLIC key.
Enable only the actions you want; read-only is enough to begin, and is the right
place to start.

You get back an API key formatted `rh-api-<uuid>`.

## 3. Save both locally

```bash
mkdir -p secrets && chmod 700 secrets
cat > secrets/robinhood_api.json <<'JSON'
{ "api_key": "rh-api-…", "private_key": "<the PRIVATE base64 from step 1>" }
JSON
chmod 600 secrets/robinhood_api.json
```

`secrets/` is git-ignored. The private key never leaves this machine, and
Robinhood will never ask for it.

## 4. Check it

```
GET  /api/v1/robinhood/probe            reachable? credentials valid?
POST /api/v1/robinhood/sync-universe    which coins are is_api_tradable
POST /api/v1/robinhood/measure-spreads  read the real spread per coin
GET  /api/v1/robinhood/quote?symbol=DOGE-USD&quantity=100
GET  /api/v1/robinhood/fee-tiers?volume_usd=0
```

## v1 vs v2 — two different cost models

**v1** routes to market makers. The quoted bid/ask already contains the spread
(0.95% per side on DOGE). v1 orders do **not** count toward fee-tier volume.

**v2** routes to partner exchanges and charges an explicit fee by trailing 30-day
volume:

| 30-day volume | taker | maker |
|---|---|---|
| $0–10K | 0.95% | 0.50% |
| $10K–50K | 0.75% | 0.35% |
| $50K–250K | **0.25%** | **0.125%** |
| $250K–500K | 0.15% | 0.075% |
| $500K–1M | 0.125% | 0.06% |

Two things follow, pointing opposite ways:

- At the entry tier, v2 taker is 0.95% — **identical** to the spread. And
  Robinhood states v2 API orders "are charged the taker rate until maker/taker is
  fully rolled out". So the API buys no cost improvement today.
- The tiers fall steeply. $50K of 30-day volume takes the round trip from 190 bps
  to **50 bps**. On a $500 account that is roughly 50 round trips a month.

The catch is that you would pay 190 bps on the way to earning 50 bps, against a
measured gross edge of 23 bps. Climbing the tiers costs more than arriving is
worth, unless the edge improves first.

## Signing, and a trap in Robinhood's own sample

```
message = f"{api_key}{timestamp}{path}{method}{body}"
```

Ed25519, base64, sent as `x-signature` with `x-api-key` and `x-timestamp`.
Timestamps expire after 30 seconds.

Robinhood's published sample signs `json.dumps(body)` — spaces after commas and
colons — then sends with `json=…`, letting the HTTP library re-serialise it
compactly. **The signed bytes and the sent bytes are different strings.** Our
client serialises once and sends that exact string as the raw body.

## Safety

`place_order` re-checks the live interlock itself and refuses unless
`TC_EXECUTION_MODE=mcp` **and** `TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`.
Read-only calls work regardless. Start with a read-only credential.
