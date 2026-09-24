"""Model registry -- the anti-black-box layer.

Rule of the project: no number reaches the screen without a card explaining
what produced it. Each card states the question, the formula, the parameters,
the assumptions, what breaks the model, and the citation. The dashboard renders
these directly, so the UI cannot drift from the truth of the code: both read
from here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field

from app.core import db


@dataclass
class ModelCard:
    name: str
    version: str
    question: str                      # the decision this model informs
    formula: str
    plain_english: str
    parameters: dict = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    breaks_when: list[str] = field(default_factory=list)
    validation: str = "not yet validated"
    status: str = "exploratory"        # exploratory | validated | deprecated
    citations: list[str] = field(default_factory=list)
    code_ref: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


CARDS: dict[str, ModelCard] = {}


def register(card: ModelCard) -> ModelCard:
    CARDS[card.name] = card
    db.execute(
        """INSERT INTO model_cards(name, version, payload_json, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(name, version) DO UPDATE SET
             payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
        (card.name, card.version, json.dumps(card.to_dict()), time.time()),
    )
    return card


def _ensure_bootstrapped() -> None:
    """Cards are the contract between the code and the UI, so they must exist
    however the app was started (uvicorn, TestClient, a script, a notebook)."""
    if not CARDS:
        bootstrap_cards()


def get(name: str) -> ModelCard | None:
    _ensure_bootstrapped()
    return CARDS.get(name)


def all_cards() -> list[dict]:
    _ensure_bootstrapped()
    return [c.to_dict() for c in sorted(CARDS.values(), key=lambda c: c.name)]


# ══════════════════════════════════════════════════════════════════════════════
# The cards
# ══════════════════════════════════════════════════════════════════════════════
def bootstrap_cards() -> None:
    register(ModelCard(
        name="execution_cost",
        version="1.0",
        question="What does one round trip on Robinhood cost, and what gross edge must a strategy beat?",
        formula=(
            "half_spread_bps = side * (fill - mid_submit)/mid_submit * 1e4;  "
            "shortfall_bps = side * (fill - mid_decision)/mid_decision * 1e4;  "
            "posterior = w*prior + (1-w)*sample_mean, w = k/(k+n), k = 8;  "
            "hurdle = 2 * per_side * safety_multiplier"
        ),
        plain_english=(
            "Every buy fills slightly above the displayed price and every sell "
            "slightly below. We measure that gap on real fills, blend it with a "
            "deliberately pessimistic prior while the sample is small, and use "
            "twice it (a round trip) times a safety margin as the minimum move "
            "any strategy must be able to predict."
        ),
        parameters={"prior_half_spread_bps": 80.0, "prior_sd_bps": 45.0,
                    "prior_pseudo_obs": 8, "min_obs_for_measurement": 20,
                    "safety_multiplier": "TC_COST_SAFETY_MULTIPLIER"},
        inputs=["fill price", "mid at submit", "mid at decision", "notional", "side"],
        outputs=["per_side_bps", "round_trip_bps", "hurdle_bps", "bootstrap CI"],
        assumptions=[
            "The reference-feed mid is a fair proxy for the true market mid at submit time.",
            "Costs are stationary over the lookback window (tested by Levene on split halves).",
            "Cost does not depend on our size -- true at $500, false at institutional size.",
        ],
        breaks_when=[
            "Robinhood changes its spread or fee structure.",
            "A coin's liquidity collapses, widening the true spread beyond the measured window.",
            "Reference feed and Robinhood diverge (a stale feed makes cost look artificially low).",
        ],
        validation="Prior only until 20 real fills exist; then measured with BCa bootstrap CI.",
        status="exploratory",
        citations=["Perold, A. (1988). The Implementation Shortfall: Paper versus Reality. JPM 14(3)."],
        code_ref="backend/app/execution/cost_model.py",
    ))

    register(ModelCard(
        name="top_mover_reversal",
        version="0.1",
        question="After a coin has moved sharply, does the next short interval tend to move back?",
        formula=(
            "z = (r_lookback - median_cross_section) / MAD_cross_section;  "
            "signal = -sign(z) if |z| > z_enter and realised_vol in [vol_lo, vol_hi];  "
            "expected_edge_bps = beta_hat * |z| estimated by robust regression on history"
        ),
        plain_english=(
            "Buy the biggest losers and sell the biggest gainers among Robinhood-"
            "tradeable coins, on the hypothesis that a sharp move overshoots. This "
            "is the classic short-horizon contrarian effect. It is a HYPOTHESIS, "
            "not a fact: it is the thing the backtester is here to falsify, and it "
            "must clear the execution-cost hurdle before it is allowed to trade."
        ),
        parameters={"lookback_minutes": 60, "z_enter": 2.0, "hold_minutes": 30,
                    "stop_loss_bps": 150, "take_profit_bps": 120,
                    "max_positions": "TC_MAX_CONCURRENT_POSITIONS"},
        inputs=["1-minute bars", "cross-sectional returns", "realised volatility", "quoted spread"],
        outputs=["side", "expected_edge_bps with CI", "reject reason if below hurdle"],
        assumptions=[
            "Cross-sectional dispersion is comparable across coins after MAD scaling.",
            "The reversal horizon is stable enough that a fixed hold time is sensible.",
            "Our own trading does not move the price (safe at $500).",
        ],
        breaks_when=[
            "The move is news-driven and trends instead of reverting (the usual way this loses).",
            "A coin is delisted, halted, or gapped while a position is open.",
            "Spreads widen exactly when dispersion is high -- i.e. precisely when the signal fires.",
        ],
        validation="Requires walk-forward out-of-sample, deflated Sharpe > gate, PBO below ceiling.",
        status="exploratory",
        citations=[
            "Lehmann, B. (1990). Fads, Martingales, and Market Efficiency. QJE 105(1).",
            "Lo, A. & MacKinlay, A.C. (1990). When Are Contrarian Profits Due to Stock Market Overreaction? RFS 3(2).",
        ],
        code_ref="backend/app/strategy/top_mover_reversal.py",
    ))

    register(ModelCard(
        name="xs_momentum",
        version="0.1",
        question="Do coins that outperformed over the last N hours keep outperforming over the next hour?",
        formula="rank coins by r_{t-N..t}; long top decile, short/avoid bottom; hold H; vol-scale positions",
        plain_english=(
            "The opposite hypothesis to reversal, at a longer horizon. Running both "
            "and letting the data choose is the honest approach: whichever survives "
            "cost-adjusted out-of-sample testing gets capital."
        ),
        parameters={"lookback_hours": 6, "hold_hours": 1, "top_k": 3, "vol_target_bps": 200},
        inputs=["hourly bars", "cross-sectional ranks", "realised volatility"],
        outputs=["side", "expected_edge_bps", "position weight"],
        assumptions=["Ranks are comparable across coins of very different volatility (hence vol scaling).",
                     "No look-ahead: ranks use only closed bars."],
        breaks_when=["Sharp market-wide reversals (momentum crashes).",
                     "Low-float coins whose 'momentum' is a single illiquid print."],
        validation="Same gate as every other strategy.",
        status="exploratory",
        citations=["Jegadeesh, N. & Titman, S. (1993). Returns to Buying Winners and Selling Losers. JF 48(1)."],
        code_ref="backend/app/strategy/xs_momentum.py",
    ))

    register(ModelCard(
        name="deflated_sharpe",
        version="1.0",
        question="Is this backtest's Sharpe ratio better than what searching this many configurations would produce by luck?",
        formula=(
            "SR0 = sqrt(V[SR]) * [(1-g)*Phi^-1(1 - 1/N) + g*Phi^-1(1 - 1/(N*e))], g = Euler-Mascheroni;  "
            "DSR = PSR(SR0) = Phi( (SR - SR0)*sqrt(n-1) / sqrt(1 - skew*SR + (kurt-1)/4*SR^2) )"
        ),
        plain_english=(
            "If you try 200 parameter sets, the best one looks good even when none "
            "of them work. This computes how good 'lucky best' would look, and asks "
            "whether the strategy beats that. Below 0.95 means: not distinguishable "
            "from luck, given how much searching we did."
        ),
        parameters={"n_trials": "counted automatically by the backtest runner"},
        inputs=["per-trade net returns", "number of configurations tested", "Sharpe variance across trials"],
        outputs=["deflated_sharpe in [0,1]", "selection-adjusted Sharpe benchmark"],
        assumptions=["Trials are drawn from a comparable universe.",
                     "Returns are iid within a trial (skew/kurtosis corrected, autocorrelation not)."],
        breaks_when=["The true number of trials is under-reported -- the most common way this is gamed.",
                     "Strongly autocorrelated returns."],
        validation="Unit-tested: near 0 on pure noise, rises with genuine edge.",
        status="validated",
        citations=["Bailey, D. & Lopez de Prado, M. (2014). The Deflated Sharpe Ratio. JPM 40(5).",
                   "Bailey, D. & Lopez de Prado, M. (2012). The Sharpe Ratio Efficient Frontier. J. Risk 15(2)."],
        code_ref="backend/app/research/stats.py::deflated_sharpe",
    ))

    register(ModelCard(
        name="pbo",
        version="1.0",
        question="If I pick the best-looking configuration, how likely is it to be below median out-of-sample?",
        formula="CSCV: split T into S chunks; for every half-split, rank the in-sample winner out-of-sample; PBO = P(logit(rank) <= 0)",
        plain_english=(
            "A direct measure of whether the selection procedure itself is "
            "overfitting. Above about 0.5 you would do better picking at random."
        ),
        parameters={"n_chunks_S": 8},
        inputs=["matrix of per-period returns, one column per configuration"],
        outputs=["pbo in [0,1]", "median logit"],
        assumptions=["Configurations are evaluated over the same periods.",
                     "Chunks are long enough to contain meaningful performance."],
        breaks_when=["Fewer than ~4*S observations.", "Highly correlated configurations (PBO becomes optimistic)."],
        validation="Unit-tested: ~0.5 on 20 noise configs, ~0.06 when one column has a real edge.",
        status="validated",
        citations=["Bailey, Borwein, Lopez de Prado & Zhu (2017). The Probability of Backtest Overfitting. JCF 20(4)."],
        code_ref="backend/app/research/stats.py::pbo_cscv",
    ))

    register(ModelCard(
        name="position_sizing",
        version="1.0",
        question="How much should we risk on one trade?",
        formula="f* = mu/sigma^2 (Kelly);  f_used = min(cap, mu_ci_low/sigma^2) * kelly_fraction",
        plain_english=(
            "Size on the LOWER end of the confidence interval for the edge, not the "
            "best guess, and then take only a quarter of that. Full Kelly on an "
            "estimated edge is how accounts die even when the strategy is right."
        ),
        parameters={"kelly_fraction": "TC_KELLY_FRACTION (hard-capped at 0.5)",
                    "cap": 0.25, "max_position_usd": "TC_MAX_POSITION_USD"},
        inputs=["posterior mean and CI of net per-trade return", "posterior variance"],
        outputs=["position notional in USD"],
        assumptions=["Returns are roughly stationary over the sizing window.",
                     "Losses are bounded by the stop (gap risk violates this)."],
        breaks_when=["An overnight gap through the stop.", "Correlated positions treated as independent."],
        validation="Unit-tested; the lower-bound rule returns 0 whenever the edge CI includes 0.",
        status="validated",
        citations=["Kelly, J.L. (1956). A New Interpretation of Information Rate. Bell System TJ 35(4).",
                   "MacLean, Thorp & Ziemba (2011). The Kelly Capital Growth Investment Criterion."],
        code_ref="backend/app/research/stats.py::kelly_with_uncertainty",
    ))

    register(ModelCard(
        name="edge_posterior",
        version="1.0",
        question="Given every trade so far, what do we now believe this strategy's net edge is?",
        formula=(
            "Normal-Inverse-Gamma conjugate update on per-trade net returns; "
            "marginal posterior of the mean is Student-t; allocation trigger is "
            "P(mean > cost_hurdle) computed from that t distribution"
        ),
        plain_english=(
            "The feedback loop's memory. Each closed trade updates a belief about "
            "the strategy's true net edge. Capital flows to strategies whose belief "
            "is confidently above the cost hurdle, and away from those that are not "
            "-- automatically, with no discretion involved."
        ),
        parameters={"prior_mu0": 0.0, "prior_kappa": 1.0, "prior_a": 2.0, "prior_b": 1e-6,
                    "allocation_rule": "Thompson sampling over posterior means, floored at the cost hurdle"},
        inputs=["net per-trade returns per strategy"],
        outputs=["posterior mean and 90% CI", "P(edge > hurdle)", "capital allocation fraction"],
        assumptions=["Trades within a strategy are exchangeable.",
                     "Net returns are approximately normal (heavy tails widen the CI, which is the safe direction)."],
        breaks_when=["A regime change makes old trades unrepresentative -- mitigated by a rolling window.",
                     "Very few trades: the posterior stays near the prior and allocation stays near zero, by design."],
        validation="Unit-tested against closed-form updates.",
        status="validated",
        citations=["Gelman et al. (2013). Bayesian Data Analysis, 3rd ed., ch. 3.",
                   "Thompson, W.R. (1933). On the Likelihood that One Unknown Probability Exceeds Another. Biometrika 25."],
        code_ref="backend/app/research/stats.py::NormalInverseGamma",
    ))

    register(ModelCard(
        name="risk_guards",
        version="1.0",
        question="What stops this from losing more than I agreed to lose?",
        formula="pre-trade checks: daily_loss, drawdown, position count, per-symbol notional, trades/day, kill switch, feed agreement",
        plain_english=(
            "Every order passes through a fixed list of hard checks before it can "
            "exist. Any single failure blocks the order and writes the reason to "
            "the audit log. The kill switch is a file on disk, so it works even if "
            "the web UI is down."
        ),
        parameters={"max_daily_loss_usd": "TC_MAX_DAILY_LOSS_USD",
                    "max_drawdown_pct": "TC_MAX_DRAWDOWN_PCT",
                    "max_position_usd": "TC_MAX_POSITION_USD",
                    "max_concurrent_positions": "TC_MAX_CONCURRENT_POSITIONS",
                    "max_trades_per_day": "TC_MAX_TRADES_PER_DAY"},
        inputs=["current equity curve", "open positions", "today's fills", "feed cross-check"],
        outputs=["allow / block + reason"],
        assumptions=["Equity is marked at the reference feed mid, which can differ from Robinhood's mark."],
        breaks_when=["The process dies holding a position -- positions are reconciled on restart, not assumed flat.",
                     "A venue halt makes exit impossible at any price."],
        validation="Unit-tested; every guard has a test that proves it blocks.",
        status="validated",
        citations=[],
        code_ref="backend/app/risk/guards.py",
    ))

    bootstrap_strategy_cards()
    bootstrap_operations_cards()
    bootstrap_breakout_cards()


# ══════════════════════════════════════════════════════════════════════════════
# Cards for the operator's four strategies and the machinery they depend on
# ══════════════════════════════════════════════════════════════════════════════
def bootstrap_strategy_cards() -> None:
    register(ModelCard(
        name="symbol_cost_estimator",
        version="1.0",
        question="What does Robinhood's spread cost on THIS coin, not on coins in general?",
        formula=("log(markup_i) = log(base) + b_spread*log(spread_i/spread_med) "
                 "+ b_vol*log(vol_i/vol_med) + b_liq*log(dv_med/dv_i);  "
                 "markup_i >= tick_bps_i;  "
                 "final = w*ballpark + (1-w)*measured_i, w = k/(k+n_i), k = 10"),
        plain_english=(
            "One coin's fill quality is not fifty coins' fill quality. BTC almost "
            "certainly costs far less to trade than a thin meme coin. This estimates "
            "each coin's Robinhood markup from things visible on a public feed -- its "
            "quoted spread, its volatility, its dollar volume and its price "
            "granularity -- and then lets real fills for that specific coin pull the "
            "number toward measurement."
        ),
        parameters={"b_spread_prior": 0.35, "b_vol_prior": 0.25, "b_liq_prior": 0.15,
                    "shrink_k": 10, "min_fills_per_coin_measured": 15,
                    "min_coins_for_coefficient_fit": 6},
        inputs=["reference quoted spread", "realised volatility", "dollar volume",
                "price/tick size", "measured fills per coin"],
        outputs=["markup_bps per coin", "round trip", "hurdle", "CI", "status label"],
        assumptions=[
            "Robinhood's embedded spread moves with the same drivers as a public market's spread.",
            "The elasticities are similar enough across coins for one set of coefficients.",
            "A coin's own fills are representative of its future fills at similar size.",
        ],
        breaks_when=[
            "Robinhood prices a coin idiosyncratically (a promotion, a listing event).",
            "The coefficients are still priors -- status 'assumed' or 'ballpark' means nobody has measured anything yet.",
            "A coin's liquidity changes faster than the 7-day observable window.",
        ],
        validation=("Coefficients are documented priors until real fills exist on at least "
                    "6 distinct coins, then refit by least squares and reported with R^2."),
        status="exploratory",
        citations=["Amihud, Y. (2002). Illiquidity and stock returns. J. Financial Markets 5(1).",
                   "Perold, A. (1988). The Implementation Shortfall. JPM 14(3)."],
        code_ref="backend/app/execution/symbol_cost.py",
    ))

    register(ModelCard(
        name="fast_flip",
        version="0.1",
        question="Can you buy something that is already moving up and get out 5-30 minutes later for a profit?",
        formula="signal = r_{t-L..t} / entry_move; expected_edge = beta_hat * signal, beta fitted on train only",
        plain_english=(
            "The simplest version of the idea, and included as the CONTROL. It is the "
            "strategy most exposed to Robinhood's spread, so running it honestly is the "
            "cheapest way to show exactly how far short it falls -- or, if it clears the "
            "hurdle, that is a real finding rather than an argument."
        ),
        parameters={"lookback_bars": 10, "hold_bars": 20, "entry_move_bps": 50,
                    "stop_bps": 120, "target_bps": 150},
        inputs=["1-minute bars", "realised volatility", "quoted spread"],
        outputs=["side", "expected edge with CI", "reject reason if below the cost hurdle"],
        assumptions=["Short-horizon momentum persists over the hold window.",
                     "The quoted move is achievable -- i.e. the spread has not already eaten it."],
        breaks_when=["The move is a single illiquid print rather than sustained buying.",
                     "The spread widens exactly when the coin starts moving, which is the norm."],
        validation="Same gates as every other strategy; expected to fail the cost gate.",
        status="exploratory",
        citations=["Jegadeesh, N. (1990). Evidence of Predictable Behavior of Security Returns. JF 45(3)."],
        code_ref="backend/app/strategy/fast_flip.py",
    ))

    register(ModelCard(
        name="forced_momentum",
        version="0.1",
        question="When a move is grinding straight up rather than thrashing, is it worth holding longer?",
        formula=("ER = |P_t - P_{t-N}| / sum|P_k - P_{k-1}|  (Kaufman efficiency ratio); "
                 "enter when ER > er_min AND volume_z > threshold AND run_length >= min; "
                 "expected_edge = beta_hat * ER"),
        plain_english=(
            "The operator's intuition that some up-moves are 'forced' -- one-directional "
            "and persistent -- while others are noise that happens to be green. The "
            "efficiency ratio measures exactly that: how much of the total distance "
            "travelled went into net progress. Volume expansion and a run of higher "
            "closes are stacked on top so that a slow illiquid drift does not qualify."
        ),
        parameters={"er_window": 30, "er_min": 0.35, "hold_bars": 45,
                    "volume_z_min": 0.5, "min_run_length": 2,
                    "stop_bps": 180, "target_bps": 260},
        inputs=["1-minute bars", "volume", "realised volatility"],
        outputs=["side", "expected edge with CI", "efficiency ratio and volume z in the features"],
        assumptions=["Efficiency measured over the last N bars carries into the next H bars.",
                     "Volume on the public feed is representative of real participation."],
        breaks_when=["The efficient move was the whole move and it reverses on completion.",
                     "News-driven spikes: maximum efficiency immediately before the top."],
        validation="Requires the full gate set in the Model Lab.",
        status="exploratory",
        citations=["Kaufman, P. (1995). Smarter Trading. McGraw-Hill."],
        code_ref="backend/app/strategy/forced_momentum.py",
    ))

    register(ModelCard(
        name="regime_swing",
        version="0.1",
        question="When the whole market is risk-on, does buying and holding for hours beat the spread?",
        formula=("risk_on = (breadth > breadth_min) AND (leader above MA) AND (MA slope > 0); "
                 "entry on positive short-horizon move within risk_on; hold H bars; "
                 "hour filter applied ONLY for hours surviving Benjamini-Hochberg"),
        plain_english=(
            "The operator's observation that when BTC wakes up the rest follows, and that "
            "during those stretches the right trade is to be in early and hold rather than "
            "scalp. The regime test requires breadth AND the leader -- one coin running is "
            "not a market. The 'early morning' part is not assumed: the hour-of-day effect "
            "is tested across all 24 hours with false-discovery-rate control, and only "
            "hours that survive are allowed to filter trades."
        ),
        parameters={"breadth_window": 120, "breadth_min": 0.60, "leader": "BTC",
                    "leader_ma_window": 200, "hold_bars": 240,
                    "stop_bps": 300, "target_bps": 500,
                    "allowed_hours": "empty unless an hour survives FDR correction"},
        inputs=["universe bars", "leader trend", "breadth", "calendar effects test"],
        outputs=["side", "expected edge with CI", "regime readings in the features"],
        assumptions=["Breadth and leader trend measured on the reference feed represent the market.",
                     "The regime persists over a multi-hour hold."],
        breaks_when=["A regime that ends the moment you join it -- the classic late entry.",
                     "Low dispersion: three positions in one regime are one position, not three.",
                     "Too few historical regime episodes to fit anything reliable."],
        validation="Gated in the Model Lab; the calendar filter is separately FDR-corrected.",
        status="exploratory",
        citations=["Sullivan, Timmermann & White (2001). Dangers of data mining: the case of "
                   "calendar effects in stock returns. J. Econometrics 105(1).",
                   "Benjamini, Y. & Hochberg, Y. (1995). Controlling the False Discovery Rate. JRSS-B 57(1)."],
        code_ref="backend/app/strategy/regime_swing.py",
    ))

    register(ModelCard(
        name="lead_lag_rotation",
        version="0.1",
        question="Do the big coins move first, and can you be early to the ones that follow?",
        formula=("for each coin and lag k: corr(leader_{t-k}, coin_t) tested against "
                 "Bartlett SE 1/sqrt(T), then Benjamini-Hochberg across all (coin, lag) pairs; "
                 "signal = leader_move - follower_move, when positive and the follower survived"),
        plain_english=(
            "The operator's observation that crypto moves in waves -- BTC and ETH, then a "
            "second tier, then the rest. If real, there is a window where the leaders have "
            "moved and a follower has not. Lead-lag is very easy to find by accident among "
            "correlated assets, so a coin only counts as a follower if its lagged "
            "correlation survives multiple-testing correction across every coin and lag tried."
        ),
        parameters={"leaders": ["BTC", "ETH"], "max_lag_bars": 10, "leader_window": 60,
                    "leader_move_min_bps": 80, "follower_lag_max_bps": 40,
                    "hold_bars": 60, "fdr_alpha": 0.10},
        inputs=["universe bars", "leader basket returns", "lagged cross-correlations"],
        outputs=["identified followers with their lags", "side", "expected edge with CI"],
        assumptions=["The lead-lag relation is stable enough to persist out of sample.",
                     "Correlation at a lag reflects information flow, not stale prices."],
        breaks_when=[
            "Thin coins whose apparent lag is just stale quotes -- the main false positive here.",
            "Everything moving at once (low dispersion), which erases the window entirely.",
            "The leader reverses before the follower catches up.",
        ],
        validation="Followers must survive FDR correction; then the gap-to-return slope must be significant.",
        status="exploratory",
        citations=["Lo, A. & MacKinlay, A.C. (1990). When Are Contrarian Profits Due to Stock "
                   "Market Overreaction? RFS 3(2).",
                   "Hayashi, T. & Yoshida, N. (2005). On covariance estimation of "
                   "non-synchronously observed diffusion processes. Bernoulli 11(2)."],
        code_ref="backend/app/strategy/lead_lag_rotation.py",
    ))

    register(ModelCard(
        name="regime_detector",
        version="1.0",
        question="Is the whole market risk-on right now, or is one coin running?",
        formula=("breadth = share of coins above their own N-bar MA; "
                 "leader_trend = (price > MA_200) and slope(MA_200) > 0; "
                 "dispersion = cross-sectional SD of returns"),
        plain_english=(
            "Three separate readings, deliberately not blended into one score, because "
            "they answer different questions. Breadth says how many coins are "
            "participating. Leader trend says whether BTC is driving. Dispersion says "
            "whether your positions are actually diversified or are secretly one bet."
        ),
        parameters={"breadth_window": 120, "leader_ma_window": 200, "slope_bars": 60,
                    "risk_on_threshold": 0.65},
        inputs=["universe close prices"],
        outputs=["breadth", "leader trend and slope", "dispersion", "risk_on boolean"],
        assumptions=["The tradeable universe is a fair sample of the crypto market."],
        breaks_when=["A universe dominated by correlated meme coins overstates breadth.",
                     "Regime flips faster than the moving-average window can register."],
        validation="Descriptive; its value is tested only through the strategies that use it.",
        status="exploratory",
        citations=[],
        code_ref="backend/app/data/regime.py",
    ))

    register(ModelCard(
        name="flow_proxy",
        version="0.1",
        question="Is unusually large size moving through this coin without moving its price?",
        formula=("volume_z = (v_t - mean(v)) / sd(v);  "
                 "amihud = mean(|r| / dollar_volume);  "
                 "kyle_lambda = slope of |r| on dollar volume;  "
                 "absorption = volume_z / kyle_lambda"),
        plain_english=(
            "The honest answer to 'can we follow the whales' is: not directly. Robinhood "
            "exposes no order flow and the public feed gives volume, not trades. What can "
            "be measured is where volume is large relative to the price move it caused, "
            "which is what accumulation looks like -- and also what several other things "
            "look like. It is a proxy, labelled as one, and it earns nothing until it "
            "shows out-of-sample predictive value."
        ),
        parameters={"window_bars": 60},
        inputs=["bar volume", "bar returns", "price"],
        outputs=["volume_z", "amihud illiquidity", "kyle lambda", "absorption ranking"],
        assumptions=["Public feed volume is representative of overall participation.",
                     "Price impact is approximately linear in size over the window."],
        breaks_when=["Wash trading or exchange-specific volume distortions.",
                     "Thin coins where a single print dominates the window.",
                     "Any situation where the inference 'low impact = accumulation' simply does not hold."],
        validation="NOT validated. No historical order flow is stored, so this cannot be backtested against ground truth.",
        status="exploratory",
        citations=["Amihud, Y. (2002). Illiquidity and stock returns. J. Financial Markets 5(1).",
                   "Kyle, A. (1985). Continuous Auctions and Insider Trading. Econometrica 53(6)."],
        code_ref="backend/app/data/regime.py::flow_pressure",
    ))

    register(ModelCard(
        name="calendar_effects",
        version="1.0",
        question="Is 'get in early in the morning' or 'memes run on weekends' a real effect?",
        formula=("Welch t-test of forward return in each hour/weekday bucket against all "
                 "other periods, then Benjamini-Hochberg FDR control across all 31 buckets"),
        plain_english=(
            "Calendar patterns are the easiest thing in finance to see when they are not "
            "there: test 31 buckets at 5% and you expect one or two to look significant by "
            "chance. This tests them all and then corrects for how many were tested, so a "
            "surviving bucket means something and a non-surviving one is a coincidence you "
            "noticed. The strategies may only filter on hours that survive."
        ),
        parameters={"horizon_bars": 30, "fdr_alpha": 0.10, "min_obs_per_bucket": 40,
                    "timezone_offset_hours": -6.0},
        inputs=["universe bars", "timestamps in the operator's local timezone"],
        outputs=["per-bucket mean forward return, p-value, survives_fdr flag"],
        assumptions=["Buckets are exchangeable across weeks.",
                     "The cross-sectional median return represents 'the market moved'."],
        breaks_when=["Too few weeks of data -- each hour bucket needs many samples.",
                     "A single dramatic day dominating one bucket."],
        validation="Method is standard and unit-tested; the findings themselves need out-of-sample confirmation.",
        status="validated",
        citations=["Benjamini, Y. & Hochberg, Y. (1995). Controlling the False Discovery Rate. JRSS-B 57(1).",
                   "Sullivan, Timmermann & White (2001). Dangers of data mining. J. Econometrics 105(1)."],
        code_ref="backend/app/research/seasonality.py",
    ))

    register(ModelCard(
        name="acceptance_gates",
        version="1.0",
        question="What has to be true before a strategy is allowed to trade?",
        formula=("all blocking gates pass: data sufficiency, calibration significance, "
                 ">= 30 out-of-sample trades, positive mean at 95%, deflated Sharpe >= 0.95, "
                 "PBO <= ceiling on >= 8 configurations, break-even cost above the venue's spread"),
        plain_english=(
            "Pre-registered thresholds, set before the results are seen, so that a "
            "good-looking equity curve is not sufficient on its own. A strategy failing "
            "any blocking gate cannot size up regardless of how attractive it looks. The "
            "gates are the difference between research and persuasion."
        ),
        parameters={"min_oos_trades": 30, "min_deflated_sharpe": 0.95,
                    "max_pbo": "TC_MAX_PBO", "min_configs_for_pbo": 8,
                    "train_validation_test": "50% / 25% / 25% with embargo"},
        inputs=["walk-forward results", "cost sensitivity curve", "calibration confidence intervals"],
        outputs=["per-gate pass/fail with the actual value", "verdict", "next action"],
        assumptions=["The number of configurations searched is reported honestly -- "
                     "under-reporting it is the standard way this is gamed."],
        breaks_when=["Parameters are tuned after looking at the test block, which silently "
                     "converts the test set into training data."],
        validation="The gates themselves are policy, not estimates; their inputs are unit-tested.",
        status="validated",
        citations=["Bailey & Lopez de Prado (2014). The Deflated Sharpe Ratio. JPM 40(5).",
                   "Lopez de Prado, M. (2018). Advances in Financial Machine Learning, ch. 7 & 11."],
        code_ref="backend/app/research/model_lab.py",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# Cards for coin selection, the trade budget, and the historical data layer
# ══════════════════════════════════════════════════════════════════════════════
def bootstrap_operations_cards() -> None:
    register(ModelCard(
        name="attention_model",
        version="0.1",
        question="Which one or two coins are getting today's attention, and is the run already over?",
        formula=(
            "attention = mean of robust z-scores of "
            "[log(dollar volume / trailing median), day range / trailing ATR, "
            "day return - cross-sectional median];  "
            "exhaustion = mean of [(price-low)/(high-low), range used / normal range, "
            "1 - recent efficiency / earlier efficiency]"
        ),
        plain_english=(
            "The operator's observation is that on a given day one or two coins take all "
            "the attention, and that by the time you notice, the run is often finished. "
            "Those are two different measurements, so they are kept separate: attention "
            "asks whether unusual activity is happening, exhaustion asks how much of the "
            "move is already behind us. High attention with LOW exhaustion is the "
            "interesting quadrant; high on both is the trap."
        ),
        parameters={"day_bars": 1440, "trailing_days": 20,
                    "exhaustion_late_threshold": 0.7, "exhaustion_mid_threshold": 0.5},
        inputs=["1-minute bars", "volume", "daily high/low", "cross-sectional returns"],
        outputs=["attention_score", "exhaustion_score", "per-coin verdict", "ranked list"],
        assumptions=[
            "Public-feed volume is representative of the attention a coin is getting.",
            "A normal day's range (trailing ATR) is a fair yardstick for 'how much has moved'.",
            "Volume and range carry as much information as return -- a coin up 9% on no "
            "volume is a print, not a move.",
        ],
        breaks_when=[
            "A coin that gaps on news: exhaustion reads high immediately even though the "
            "move may continue for days.",
            "Wash trading inflating the volume component.",
            "Fewer than ~20 trailing days, which makes every z-score unstable.",
        ],
        validation=("Ranking only. Its value is tested through the strategies that consume "
                    "it, and through leadership_persistence, which checks directly whether "
                    "yesterday's leader leads again."),
        status="exploratory",
        citations=["Barber, B. & Odean, T. (2008). All That Glitters: The Effect of Attention "
                   "on the Buying Behavior of Individual and Institutional Investors. RFS 21(2)."],
        code_ref="backend/app/data/attention.py",
    ))

    register(ModelCard(
        name="trade_budget",
        version="1.0",
        question="I want at most N trades in the next 24 hours — which N should they be?",
        formula=(
            "V(k,s) = (1-p)V(k-1,s) + p·E[max(q + V(k-1,s-1), V(k-1,s))];  "
            "theta(k,s) = V(k-1,s) - V(k-1,s-1);  take a signal iff quality >= theta"
        ),
        plain_english=(
            "Taking the first N signals that clear the hurdle is almost always wrong — the "
            "first signal of the day is rarely the best one. This solves the actual problem: "
            "with s slots left and k time steps to go, hold out for anything better than a "
            "reservation threshold. The threshold falls as the deadline approaches, because "
            "a slot you never spend is worth nothing, and it rises when you have few slots "
            "and lots of time. It is computed from the empirical distribution of recent "
            "signal quality, so it adapts to a quiet day instead of sitting on its hands."
        ),
        parameters={"time_steps": 48, "min_quality_samples": 30,
                    "quality_metric": "expected edge minus the per-coin cost hurdle, in bps",
                    "quality_lookback_days": 14},
        inputs=["recent signal expected-edge and hurdle values", "signal arrival rate",
                "slots remaining", "time remaining in the window"],
        outputs=["current threshold in bps", "take/hold decision with the reason"],
        assumptions=[
            "Signal quality is drawn from a stable distribution over the window.",
            "Signals arrive roughly independently at a constant rate.",
            "Recent history is representative of today -- false on an unusual day.",
        ],
        breaks_when=[
            "Fewer than 30 historical signals: the threshold is undefined and the budget "
            "deliberately stops filtering rather than guessing.",
            "A regime change mid-window, which makes the learned distribution stale.",
            "Clustered signals (all five arrive in one hour), which violates independence.",
        ],
        validation="Dynamic program is unit-tested for monotonicity in both slots and time.",
        status="validated",
        citations=[
            "Albright, S.C. (1974). Optimal Sequential Assignments with Random Arrival Times. "
            "Management Science 21(1).",
            "Ferguson, T. (1989). Who Solved the Secretary Problem? Statistical Science 4(3).",
        ],
        code_ref="backend/app/execution/budget.py",
    ))

    register(ModelCard(
        name="historical_data",
        version="1.0",
        question="How much real history can we get, and is my laptop big enough to hold it?",
        formula="paginated Coinbase candle windows of 300 bars, walked backwards until empty",
        plain_english=(
            "A live feed serves a few hundred recent candles, which is why every strategy "
            "started out data-starved. Walking the start/end window backwards gives years "
            "of history for free. What is available depends on resolution: daily goes back "
            "to each coin's listing, hourly gives several years, 15-minute one to two years, "
            "and one-minute only weeks — nobody backfills minute data, it is collected going "
            "forward. That last fact is a real constraint on the fast strategies and an "
            "argument for working on the longer-horizon ones first."
        ),
        parameters={"max_candles_per_request": 300, "rate_limit_sleep_s": 0.12,
                    "granularities": [60, 300, 900, 3600, 21600, 86400]},
        inputs=["Coinbase Exchange public candles endpoint", "the tradeable universe"],
        outputs=["stored bars", "coverage per symbol and granularity", "storage projections"],
        assumptions=[
            "Coinbase prices are a fair research proxy for Robinhood's prices; the DIFFERENCE "
            "between them is measured separately as execution cost, not ignored.",
            "A coin's history on Coinbase is representative of its history on Robinhood, "
            "which is false around listing dates that differ between venues.",
        ],
        breaks_when=[
            "A coin tradeable on Robinhood but not listed on Coinbase — it simply gets no history.",
            "Exchange outages leaving gaps; the coverage report shows completeness per symbol "
            "so gaps are visible rather than silently interpolated.",
        ],
        validation="Storage projections use the measured bytes-per-row of this database, "
                   "not a textbook estimate.",
        status="validated",
        citations=[],
        code_ref="backend/app/data/backfill.py",
    ))


def bootstrap_breakout_cards() -> None:
    register(ModelCard(
        name="false_breakout",
        version="1.0",
        question="This level just broke — is the move real, or is it a trap?",
        formula=(
            "primary: close crosses a knowable level by > 0.15 x ATR -> an EVENT;  "
            "label (triple barrier): +max(1 ATR, 200 bps) first = genuine, back through "
            "the level first = false, else resolved at the horizon;  "
            "secondary: logistic regression on 21 features fitted by IRLS with L2, "
            "P(false) used as a veto"
        ),
        plain_english=(
            "Breaking a level is where resting stop orders sit, which is exactly why "
            "breaking it produces guaranteed liquidity for someone who wants to sell "
            "into it. Most breaks fail. This separates the mechanical question (did a "
            "level break?) from the hard one (is this one real?), which is the "
            "meta-labelling pattern, and answers the second with a calibrated "
            "probability rather than a rule of thumb."
        ),
        parameters={
            "buffer_atr": 0.15, "level_lookback_bars": 720, "min_gap_bars": 30,
            "label_horizon_bars": 120, "label_target": "max(1 ATR, 200 bps)",
            "fail_buffer_atr": 0.1, "l2": 1.0, "veto_threshold": 0.65,
            "n_features": 21,
        },
        inputs=[
            "pivot / round-number / volume-node levels knowable at the break bar",
            "close location and wick of the break bar", "volume z-score and 3-bar trend",
            "approach efficiency", "level touch count and prior failures",
            "distance from VWAP", "position in the day's range", "ATR expansion",
            "room to the next level", "relative strength, breadth, leader return",
            "spread at the break", "hour of day",
        ],
        outputs=["P(false breakout)", "block / allow", "the five features driving this call"],
        assumptions=[
            "Levels computed from public candles approximate where real orders rest.",
            "The triple-barrier definition of 'genuine' (a move big enough to pay for a "
            "round trip) matches what we would actually try to monetise.",
            "The relationship between features and failure is stable enough to persist "
            "out of sample — tested by purged walk-forward, not assumed.",
        ],
        breaks_when=[
            "Fewer than ~60 labelled events, where the model refuses to exist at all.",
            "A regime change: what made breaks fail in a chop does not apply in a trend.",
            "News-driven breaks, which are outside the feature set entirely — this is why "
            "the news module carries a separate hard veto.",
            "Level detection lag: a pivot is only knowable some bars after it forms, and "
            "the code enforces that, so very recent structure is invisible by construction.",
        ],
        validation=(
            "Purged, embargoed walk-forward with AUC, Brier vs the base rate, log loss, "
            "and a reliability table. The veto is DISABLED unless the bootstrap 95% CI on "
            "out-of-sample AUC is entirely above 0.5. A model that cannot beat chance is "
            "not allowed to block a trade."
        ),
        status="exploratory",
        citations=[
            "Lopez de Prado, M. (2018). Advances in Financial Machine Learning, ch. 3 "
            "(meta-labelling, triple barrier) and ch. 7 (purged cross-validation).",
            "Osler, C. (2003). Currency Orders and Exchange Rate Dynamics. JF 58(5).",
            "Brier, G. (1950). Verification of Forecasts Expressed in Terms of Probability.",
            "Platt, J. (1999). Probabilistic Outputs for Support Vector Machines.",
        ],
        code_ref="backend/app/research/breakout.py",
    ))

    register(ModelCard(
        name="news_events",
        version="0.1",
        question="Is something happening to this coin that makes its statistics irrelevant?",
        formula="RSS ingestion -> symbol matching on name aliases -> keyword classification into event categories with a severity rank",
        plain_english=(
            "News is context, not signal. By the time a headline reaches an RSS feed it "
            "has been in the market for seconds to minutes and the fast money has traded "
            "it. What it IS good for is defence: a coin being delisted, drained or sued "
            "is not a coin whose mean-reversion statistics apply. That defensive use is "
            "the only one this module claims."
        ),
        parameters={"sources": 5, "categories": 10,
                    "severities": ["info", "warning", "serious", "critical"],
                    "alert_window_hours": 24},
        inputs=["public RSS feeds", "coin name aliases", "keyword patterns"],
        outputs=["recent headlines per coin", "event categories", "severity", "flagged symbols"],
        assumptions=["Headline keywords are a reasonable proxy for event type.",
                     "Name matching catches the coin — it will miss ticker-only mentions "
                     "for short symbols, deliberately, to avoid false matches."],
        breaks_when=["A feed changes format or goes down (errors are reported, not hidden).",
                     "Ambiguous names (a coin sharing a word with ordinary English).",
                     "Anything requiring the CONTENT of the article rather than its headline."],
        validation="Not a predictive model and not validated as one. Used only as a hard veto.",
        status="exploratory",
        citations=[],
        code_ref="backend/app/data/news.py",
    ))
