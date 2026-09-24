"""Run this first on your Mac: it checks feeds, database, models and statistics
end to end, without touching a broker.

    cd backend && .venv/bin/python selftest.py
"""
from __future__ import annotations

import sys
import traceback

import numpy as np

sys.path.insert(0, ".")

from app.config import get_settings          # noqa: E402
from app.core import db, registry            # noqa: E402
from app.data import universe                # noqa: E402
from app.data.feeds import cross_check, get_fallback_feed, get_feed  # noqa: E402
from app.execution import cost_model         # noqa: E402
from app.research import stats as S          # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((True, name, detail))
        print(f"{PASS}  {name}: {detail}")
    except Exception as exc:
        results.append((False, name, f"{type(exc).__name__}: {exc}"))
        print(f"{FAIL}  {name}: {type(exc).__name__}: {exc}")
        if "-v" in sys.argv:
            traceback.print_exc()


def main() -> int:
    s = get_settings()
    print(f"\nTradeCrypto self-test -- mode={s.execution_mode}, live_enabled={s.live_enabled}\n")

    check("database", lambda: f"initialised at {db.init_db()}")
    check("model cards", lambda: f"{len(registry.all_cards()) or (registry.bootstrap_cards() or len(registry.all_cards()))} registered")

    def _feed():
        f = get_feed()
        prods = f.products()
        q = f.quote(prods["BTC"])
        return f"{f.name}: {len(prods)} USD pairs, BTC mid {q.mid:,.2f}, spread {q.spread_bps:.1f} bps"
    check("primary feed", _feed)

    def _cross():
        f, fb = get_feed(), get_fallback_feed()
        a = f.quote(f.products()["BTC"])
        b = fb.quote(fb.products()["BTC"])
        r = cross_check("BTC", a, b)
        return f"{a.source} vs {b.source} differ by {r['diff_bps']:.1f} bps -- agree={r['agree']}"
    check("feed cross-check", _cross)

    check("universe refresh", lambda: str(universe.refresh_universe()["priceable"]) + " symbols priceable")

    def _candles():
        f = get_feed()
        bars = f.candles(f.products()["BTC"], granularity=60, limit=300)
        return f"{len(bars)} 1-minute BTC candles, latest close {bars[-1].close:,.2f}"
    check("historical candles", _candles)

    def _cost():
        e = cost_model.estimate()
        return (f"per-side {e.per_side_bps:.0f} bps ({e.source}, n={e.n_observations}), "
                f"round trip {e.round_trip_bps/100:.2f}%, hurdle {e.hurdle_bps/100:.2f}%")
    check("cost model", _cost)

    def _stats():
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 0.01, 400)
        dsr = S.deflated_sharpe(noise, n_trials=50)["deflated_sharpe"]
        X = rng.normal(0, 0.01, size=(400, 12))
        pbo = S.pbo_cscv(X)["pbo"]
        assert dsr < 0.5, "deflated Sharpe should reject pure noise"
        assert 0.2 < pbo < 0.8, "PBO on noise should be near 0.5"
        return f"noise rejected: deflated Sharpe {dsr:.3f}, PBO {pbo:.2f}"
    check("statistics sanity", _stats)

    def _sizing():
        f = S.kelly_with_uncertainty(-0.001, 0.0004)
        assert f == 0.0, "must not size when the edge CI includes zero"
        return "refuses to size a strategy whose edge lower bound is negative"
    check("position sizing guard", _sizing)

    n_fail = sum(1 for okk, _, _ in results if not okk)
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed\n")
    if n_fail:
        print("If feed checks failed, you are probably offline or the exchange is "
              "rate-limiting; everything else runs without network.\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
