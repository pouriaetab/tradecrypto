# Connecting Robinhood — the bridge, and why it works this way

## The situation

Robinhood's agentic endpoint is an **OAuth-gated MCP server** built for an interactive
LLM client. It is not a keyed REST API, so the Python process behind this dashboard
cannot call it directly — there is no credential it could hold.

So the bridge runs the other way round. A **Claude Code session that has the MCP
connected** reads Robinhood and POSTs what it finds into this app's local API. The app
never touches Robinhood; the agent session never places an order on this path.

## One-time setup

You have already done this:

```bash
claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
```

Now finish the authentication:

1. In that terminal, in the `tradecrypto` folder, run `claude`
2. Type `/mcp`
3. Select `robinhood-trading`
4. Complete the browser OAuth — **never type your Robinhood password into any tool**

Sanity check before anything else: ask it *"list my positions in the agentic account"*.
A successful read confirms the connection. Do not attempt a write.

## Sync 1 — which coins are actually tradeable

Make sure TradeCrypto is running (`./run.sh`), then paste this into that Claude Code session:

> Using the robinhood-trading MCP, list every cryptocurrency symbol I can trade.
> Return only the base symbols (BTC, ETH, DOGE, …), not pairs.
> Then POST them to my local app, exactly like this, and show me the response:
>
> ```
> curl -s -X POST http://127.0.0.1:8006/api/v1/universe/confirm \
>   -H 'content-type: application/json' \
>   -d '{"symbols": ["BTC","ETH", ...], "deactivate_missing": true}'
> ```
>
> Do not place any orders. Do not modify any account.

Every confirmed symbol turns from **unverified** to **confirmed** in Movers and the
Cost Lab. Live mode refuses anything still unverified, so this sync is a prerequisite
for real trading — and harmless before it.

## Sync 2 — your real fills (this is the valuable one)

The cost model is still leaning on a prior. Real fills replace belief with measurement,
and Robinhood already has your order history:

> Using the robinhood-trading MCP, pull my crypto order history for the last 90 days.
> For each filled order give me: symbol, side, notional in dollars, the average fill
> price, and the timestamp. Then POST them to my local app as:
>
> ```
> curl -s -X POST http://127.0.0.1:8006/api/v1/cost/observations/bulk \
>   -H 'content-type: application/json' \
>   -d '{"observations": [{"symbol":"DOGE","side":"buy","notional_usd":25,
>        "fill_px":0.2478,"mid_at_submit":0.2450}, ...]}'
> ```
>
> Do not place any orders.

`mid_at_submit` is the reference price at the moment of the order. If the MCP cannot
give you that, use the minute bar's close from the Data page for that timestamp — it is
an approximation, and the resulting observation is still far better than the prior.

Once six or more distinct coins have real fills, the per-coin cost model stops using
assumed elasticities and fits them, and the Cost Lab will say so.

## What this bridge deliberately cannot do

- It cannot place, cancel or modify an order.
- It cannot move money.
- It does not store any Robinhood credential; the OAuth token stays inside your
  Claude Code MCP configuration, not in this project.

## Whether you can trade the Agentic account by hand

Undocumented, and reports disagree — one review even claims crypto is not yet supported
for agents at all, contradicting Robinhood's own support pages. **Do not build on it.**
The plan does not need it: strategies run in paper mode, and if you want real fill data
for the cost model, place small trades in your **main** account and log them in Cost Lab
or through Sync 2 above.
