# Read this before you do anything else

**This is a personal research project, shared as-is. It is not a product, not a
service, and not advice.**

## It loses money

This is not modesty and it is not a disclaimer written by a lawyer. It is the
measured result, and the app is built to show it to you rather than hide it:

- A round trip on Robinhood costs about **1.9%** — roughly 0.95% on the way in
  and again on the way out, taken in the spread rather than as a fee.
- Most of the coins it watches move **2–3% in a day**.
- So a trade has to be right by more than the whole day's typical move before it
  earns a single cent.

Run the demo and look at the numbers. Over a month of real replayed prices the
book is **down**, with roughly **40% winners**. The author's own live paper
record says the same thing. There is no version of this where the losses are a
bug that is about to be fixed — the cost is the finding.

## It is paper trading by default, and should stay that way

Out of the box it places **no real orders**. It watches real prices and pretends.
Nothing can be lost.

Making it trade real money takes **three separate deliberate acts** by whoever
is running it:

1. Setting `TC_EXECUTION_MODE=mcp` in `.env`
2. Typing the exact phrase `I_ACCEPT_REAL_MONEY_RISK` into `TC_LIVE_CONFIRM`
3. Supplying their own Robinhood API credentials

No button in the interface can do any of these. That is deliberate. **If you
perform those three acts, you are choosing to risk your own money, and the
outcome is yours.**

## No warranty, no liability

The software is provided under the MIT License (see `LICENSE`), **"AS IS",
WITHOUT WARRANTY OF ANY KIND**, and the author is **not liable** for any claim,
damages or other liability arising from it — including trading losses.

## It is not financial advice

Nothing here is a recommendation to buy, sell or hold anything. The author is
not a financial adviser, is not registered as one, and is not acting as one.
Nothing in this repository is personalised advice to anyone.

## It has bugs

It is a personal project. Bugs have been found in it that produced wrong
numbers, wrong trades and silent failures, and more will be. It carries no
support, no uptime expectation and no guarantee that any figure on any screen is
correct.

## If you are going to use it anyway

- Keep it in paper mode. That is what it is for.
- If you will not, then risk only money you are fully prepared to lose entirely.
- Read the Cost page before the Overview page. It explains why the spread, not
  the strategy, is the thing that decides the outcome.
