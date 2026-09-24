"""The research library — what has been read, and what it is actually used for.

Two rules keep this honest:

  1. Nothing is listed as APPLIED unless a specific line of code in this project
     implements it. The `used_for` field names the file.
  2. QUEUED means exactly that — read but not implemented, or not yet read. It is
     not a claim of expertise; it is a reading list with reasons.

The scope is deliberately wide, because the useful ideas rarely arrive from
finance journals alone: the calibration methods come from machine learning, the
multiple-testing discipline from biostatistics, and the sequence models from
NLP. Entries outside finance carry a note saying why they might matter here.
"""
from __future__ import annotations

import json
import time

from app.core import db

APPLIED = "applied"
READ = "read"
QUEUED = "queued"

PAPERS: list[dict] = [
    # ── inference and the fooling-yourself problem ───────────────────────────
    dict(key="bailey2014dsr", title="The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
         authors="Bailey, D. & Lopez de Prado, M.", year=2014, venue="Journal of Portfolio Management 40(5)",
         url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551",
         one_line="Corrects a Sharpe ratio for how many strategy configurations you searched before finding it.",
         used_for="research/stats.py::deflated_sharpe — the gate every strategy must clear in Model Lab.",
         tags=["statistics", "backtesting"], status=APPLIED),
    dict(key="bailey2012psr", title="The Sharpe Ratio Efficient Frontier",
         authors="Bailey, D. & Lopez de Prado, M.", year=2012, venue="Journal of Risk 15(2)",
         url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643",
         one_line="Probabilistic Sharpe Ratio: the probability a true Sharpe exceeds a benchmark, adjusted for skew and kurtosis.",
         used_for="research/stats.py::probabilistic_sharpe and min_track_record_length.",
         tags=["statistics"], status=APPLIED),
    dict(key="bailey2017pbo", title="The Probability of Backtest Overfitting",
         authors="Bailey, Borwein, Lopez de Prado & Zhu", year=2017, venue="Journal of Computational Finance 20(4)",
         url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253",
         one_line="Measures how often the best in-sample configuration lands below median out of sample.",
         used_for="research/stats.py::pbo_cscv — reported in every walk-forward, with an explicit 'unreliable below 8 configurations' guard.",
         tags=["statistics", "backtesting"], status=APPLIED),
    dict(key="ldp2018afml", title="Advances in Financial Machine Learning",
         authors="Lopez de Prado, M.", year=2018, venue="Wiley",
         url="https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086",
         one_line="Meta-labelling, the triple-barrier method, purged and embargoed cross-validation.",
         used_for="research/breakout.py (meta-labelling + triple barrier) and research/backtest.py (purged walk-forward with embargo).",
         tags=["machine learning", "backtesting"], status=APPLIED),
    dict(key="harvey2016", title="... and the Cross-Section of Expected Returns",
         authors="Harvey, C., Liu, Y. & Zhu, H.", year=2016, venue="Review of Financial Studies 29(1)",
         url="https://academic.oup.com/rfs/article/29/1/5/1843824",
         one_line="Hundreds of published factors; most would not survive a multiple-testing correction.",
         used_for="The reason every discovery path here is FDR-corrected rather than reported at raw p<0.05.",
         tags=["statistics"], status=READ),
    dict(key="bh1995", title="Controlling the False Discovery Rate",
         authors="Benjamini, Y. & Hochberg, Y.", year=1995, venue="JRSS-B 57(1)",
         url="https://www.jstor.org/stable/2346101",
         one_line="Control the expected proportion of false positives among the discoveries you act on.",
         used_for="research/seasonality.py (24 hours x 7 days) and strategy/lead_lag_rotation.py (every coin x lag pair).",
         tags=["statistics"], status=APPLIED),
    dict(key="sullivan2001", title="Dangers of Data Mining: The Case of Calendar Effects in Stock Returns",
         authors="Sullivan, R., Timmermann, A. & White, H.", year=2001, venue="Journal of Econometrics 105(1)",
         url="https://www.sciencedirect.com/science/article/abs/pii/S0304407601000774",
         one_line="Calendar effects mostly vanish once you account for how many calendars were tried.",
         used_for="Why the 'early morning / dries up by 4-5pm' belief is tested with FDR control instead of hard-coded.",
         tags=["statistics"], status=APPLIED),
    dict(key="white2000", title="A Reality Check for Data Snooping",
         authors="White, H.", year=2000, venue="Econometrica 68(5)",
         url="https://www.jstor.org/stable/2999444",
         one_line="Bootstrap test for whether the best of many strategies beats a benchmark.",
         used_for="Queued: a stronger alternative to the deflated Sharpe when the strategy count grows.",
         tags=["statistics"], status=QUEUED),
    dict(key="hansen2005", title="A Test for Superior Predictive Ability",
         authors="Hansen, P.R.", year=2005, venue="Journal of Business & Economic Statistics 23(4)",
         url="https://www.jstor.org/stable/27638834",
         one_line="Improves on White's Reality Check by removing the drag of poor candidate strategies.",
         used_for="Queued alongside White (2000).",
         tags=["statistics"], status=QUEUED),

    # ── resampling ───────────────────────────────────────────────────────────
    dict(key="politis1994", title="The Stationary Bootstrap",
         authors="Politis, D. & Romano, J.", year=1994, venue="JASA 89(428)",
         url="https://www.jstor.org/stable/2290993",
         one_line="Resample time series in geometric blocks so short-range dependence survives.",
         used_for="research/stats.py::stationary_bootstrap_indices — every confidence interval on autocorrelated returns.",
         tags=["statistics"], status=APPLIED),
    dict(key="efron1987", title="Better Bootstrap Confidence Intervals",
         authors="Efron, B.", year=1987, venue="JASA 82(397)",
         url="https://www.jstor.org/stable/2289144",
         one_line="BCa intervals correct for bias and skew in the bootstrap distribution.",
         used_for="research/stats.py::bootstrap_ci — the intervals shown next to every cost and edge estimate.",
         tags=["statistics"], status=APPLIED),
    dict(key="white1980", title="A Heteroskedasticity-Consistent Covariance Matrix Estimator",
         authors="White, H.", year=1980, venue="Econometrica 48(4)",
         url="https://www.jstor.org/stable/1912934",
         one_line="Standard errors that survive non-constant error variance.",
         used_for="strategy/_fitting.py::fit_slope — crypto variance is anything but constant across the signal range.",
         tags=["statistics"], status=APPLIED),

    # ── execution and microstructure ─────────────────────────────────────────
    dict(key="perold1988", title="The Implementation Shortfall: Paper versus Reality",
         authors="Perold, A.", year=1988, venue="Journal of Portfolio Management 14(3)",
         url="https://jpm.pm-research.com/content/14/3/4",
         one_line="Measure trading cost as the gap between the decision price and the realised fill.",
         used_for="execution/cost_model.py — the definition of shortfall in the Cost Lab.",
         tags=["execution"], status=APPLIED),
    dict(key="kyle1985", title="Continuous Auctions and Insider Trading",
         authors="Kyle, A.", year=1985, venue="Econometrica 53(6)",
         url="https://www.jstor.org/stable/1913210",
         one_line="Lambda: how much price moves per unit of order flow.",
         used_for="data/regime.py::flow_pressure — the absorption proxy, explicitly labelled as a proxy.",
         tags=["microstructure"], status=APPLIED),
    dict(key="amihud2002", title="Illiquidity and Stock Returns: Cross-Section and Time-Series Effects",
         authors="Amihud, Y.", year=2002, venue="Journal of Financial Markets 5(1)",
         url="https://www.sciencedirect.com/science/article/abs/pii/S1386418101000246",
         one_line="A simple, robust illiquidity measure: absolute return per dollar of volume.",
         used_for="data/regime.py flow proxy and execution/symbol_cost.py liquidity elasticity.",
         tags=["microstructure", "execution"], status=APPLIED),
    dict(key="almgren2000", title="Optimal Execution of Portfolio Transactions",
         authors="Almgren, R. & Chriss, N.", year=2000, venue="Journal of Risk 3(2)",
         url="https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf",
         one_line="Trade off market impact against timing risk when working an order.",
         used_for="Queued — irrelevant at $500 notional, relevant if size ever grows.",
         tags=["execution"], status=QUEUED),
    dict(key="easley2012vpin", title="Flow Toxicity and Liquidity in a High-Frequency World",
         authors="Easley, D., Lopez de Prado, M. & O'Hara, M.", year=2012, venue="Review of Financial Studies 25(5)",
         url="https://academic.oup.com/rfs/article/25/5/1457/1569929",
         one_line="VPIN: volume-synchronised probability of informed trading, an early warning of toxic flow.",
         used_for="Queued — needs trade-level data the public candle feed does not provide.",
         tags=["microstructure"], status=QUEUED),
    dict(key="cont2014", title="The Price Impact of Order Book Events",
         authors="Cont, R., Kukanov, A. & Stoikov, S.", year=2014, venue="Journal of Financial Econometrics 12(1)",
         url="https://academic.oup.com/jfec/article/12/1/47/815722",
         one_line="Order flow imbalance explains short-horizon price moves better than volume does.",
         used_for="Queued — would replace the volume proxy if order book data ever becomes available.",
         tags=["microstructure"], status=QUEUED),

    # ── the strategies themselves ────────────────────────────────────────────
    dict(key="lehmann1990", title="Fads, Martingales, and Market Efficiency",
         authors="Lehmann, B.", year=1990, venue="Quarterly Journal of Economics 105(1)",
         url="https://www.jstor.org/stable/2937816",
         one_line="Short-horizon reversal: securities that moved sharply tend to move back.",
         used_for="strategy/top_mover_reversal.py — the hypothesis behind fading the day's biggest movers.",
         tags=["strategy"], status=APPLIED),
    dict(key="lomackinlay1990", title="When Are Contrarian Profits Due to Stock Market Overreaction?",
         authors="Lo, A. & MacKinlay, A.C.", year=1990, venue="Review of Financial Studies 3(2)",
         url="https://academic.oup.com/rfs/article/3/2/175/1585438",
         one_line="Much of contrarian profit is cross-autocorrelation — some assets lead others — not overreaction.",
         used_for="strategy/lead_lag_rotation.py — the statistical basis for the 'batches move in waves' idea.",
         tags=["strategy"], status=APPLIED),
    dict(key="jegadeesh1993", title="Returns to Buying Winners and Selling Losers",
         authors="Jegadeesh, N. & Titman, S.", year=1993, venue="Journal of Finance 48(1)",
         url="https://www.jstor.org/stable/2328882",
         one_line="Cross-sectional momentum: past winners keep winning over intermediate horizons.",
         used_for="strategy/xs_momentum.py — deliberately run against the reversal hypothesis under identical gates.",
         tags=["strategy"], status=APPLIED),
    dict(key="kaufman1995", title="Smarter Trading (efficiency ratio)",
         authors="Kaufman, P.", year=1995, venue="McGraw-Hill",
         url="https://www.google.com/search?q=Kaufman+Smarter+Trading+efficiency+ratio",
         one_line="Net travel divided by total path: how directional a move actually was.",
         used_for="strategy/forced_momentum.py and research/breakout.py — the formal version of 'the move is being forced'.",
         tags=["strategy"], status=APPLIED),
    dict(key="osler2003", title="Currency Orders and Exchange Rate Dynamics",
         authors="Osler, C.", year=2003, venue="Journal of Finance 58(5)",
         url="https://onlinelibrary.wiley.com/doi/10.1046/j.1540-6261.2003.00609.x",
         one_line="Stop-loss orders cluster at round numbers, which is why breaks there accelerate and then reverse.",
         used_for="research/breakout.py::round_levels — the mechanism behind the false-breakout trap.",
         tags=["microstructure", "strategy"], status=APPLIED),
    dict(key="barber2008", title="All That Glitters: The Effect of Attention on the Buying Behavior of Individual and Institutional Investors",
         authors="Barber, B. & Odean, T.", year=2008, venue="Review of Financial Studies 21(2)",
         url="https://academic.oup.com/rfs/article/21/2/785/1596741",
         one_line="Retail investors buy what is attention-grabbing, and attention is measurable through volume and extreme returns.",
         used_for="data/attention.py — the 'one or two coins take the day' ranking.",
         tags=["behavioural"], status=APPLIED),

    # ── sizing, allocation, decision ─────────────────────────────────────────
    dict(key="kelly1956", title="A New Interpretation of Information Rate",
         authors="Kelly, J.L.", year=1956, venue="Bell System Technical Journal 35(4)",
         url="https://ieeexplore.ieee.org/document/6771227",
         one_line="The growth-optimal betting fraction given a known edge.",
         used_for="research/stats.py::kelly_with_uncertainty — sized on the edge's LOWER confidence bound, at quarter-Kelly.",
         tags=["sizing"], status=APPLIED),
    dict(key="maclean2011", title="The Kelly Capital Growth Investment Criterion",
         authors="MacLean, L., Thorp, E. & Ziemba, W.", year=2011, venue="World Scientific",
         url="https://www.worldscientific.com/worldscibooks/10.1142/7598",
         one_line="Why fractional Kelly is what practitioners actually use: full Kelly assumes an edge you do not have.",
         used_for="config.py — the hard cap of 0.5 on TC_KELLY_FRACTION.",
         tags=["sizing"], status=APPLIED),
    dict(key="albright1974", title="Optimal Sequential Assignments with Random Arrival Times",
         authors="Albright, S.C.", year=1974, venue="Management Science 21(1)",
         url="https://pubsonline.informs.org/doi/10.1287/mnsc.21.1.60",
         one_line="With a fixed number of slots and a deadline, accept only above a reservation threshold.",
         used_for="execution/budget.py — 'take 2 trades today' without spending them on the first two signals.",
         tags=["decision"], status=APPLIED),
    dict(key="ferguson1989", title="Who Solved the Secretary Problem?",
         authors="Ferguson, T.", year=1989, venue="Statistical Science 4(3)",
         url="https://www.jstor.org/stable/2245639",
         one_line="The history and correct formulations of optimal stopping with a deadline.",
         used_for="Background for the trade-budget dynamic program.",
         tags=["decision"], status=READ),
    dict(key="thompson1933", title="On the Likelihood that One Unknown Probability Exceeds Another",
         authors="Thompson, W.R.", year=1933, venue="Biometrika 25(3/4)",
         url="https://www.jstor.org/stable/2332286",
         one_line="Sample from the posterior to balance exploration against exploitation.",
         used_for="feedback/loop.py — allocating capital across strategies from their edge posteriors.",
         tags=["decision", "bayesian"], status=APPLIED),
    dict(key="gelman2013", title="Bayesian Data Analysis (3rd ed.)",
         authors="Gelman, A. et al.", year=2013, venue="CRC Press",
         url="http://www.stat.columbia.edu/~gelman/book/",
         one_line="Conjugate updating, hierarchical models and partial pooling.",
         used_for="research/stats.py (Normal-Inverse-Gamma, Beta-Binomial) and execution/symbol_cost.py (shrinkage across coins).",
         tags=["bayesian"], status=APPLIED),

    # ── calibration and evaluation ───────────────────────────────────────────
    dict(key="brier1950", title="Verification of Forecasts Expressed in Terms of Probability",
         authors="Brier, G.", year=1950, venue="Monthly Weather Review 78(1)",
         url="https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml",
         one_line="A proper scoring rule for probabilistic forecasts.",
         used_for="research/breakout.py — scoring the false-breakout model against the base rate.",
         tags=["machine learning"], status=APPLIED),
    dict(key="platt1999", title="Probabilistic Outputs for Support Vector Machines",
         authors="Platt, J.", year=1999, venue="Advances in Large Margin Classifiers",
         url="https://www.cs.colorado.edu/~mozer/Teaching/syllabi/6622/papers/Platt1999.pdf",
         one_line="Turning a classifier score into a calibrated probability.",
         used_for="research/breakout.py::calibration — the reliability check that makes the veto threshold meaningful.",
         tags=["machine learning"], status=APPLIED),
    dict(key="ljungbox1978", title="On a Measure of Lack of Fit in Time Series Models",
         authors="Ljung, G. & Box, G.", year=1978, venue="Biometrika 65(2)",
         url="https://www.jstor.org/stable/2335207",
         one_line="Portmanteau test for residual autocorrelation.",
         used_for="research/stats.py::assumption_tests — flags when iid confidence intervals would be too narrow.",
         tags=["statistics"], status=APPLIED),

    # ── crypto specifically ──────────────────────────────────────────────────
    dict(key="makarov2020", title="Trading and Arbitrage in Cryptocurrency Markets",
         authors="Makarov, I. & Schoar, A.", year=2020, venue="Journal of Financial Economics 135(2)",
         url="https://www.sciencedirect.com/science/article/abs/pii/S0304405X19301746",
         one_line="Large, persistent cross-exchange price gaps — and why capital controls and frictions keep them open.",
         used_for="Why this system cross-checks two venues before trusting a price, and treats the venue gap as cost.",
         tags=["crypto"], status=READ),
    dict(key="liu2021", title="Risks and Returns of Cryptocurrency",
         authors="Liu, Y. & Tsyvinski, A.", year=2021, venue="Review of Financial Studies 34(6)",
         url="https://academic.oup.com/rfs/article/34/6/2689/5912024",
         one_line="Crypto returns are driven by their own momentum and investor-attention factors, not standard macro risks.",
         used_for="Support for treating attention and momentum as first-class features rather than macro exposures.",
         tags=["crypto"], status=READ),
    dict(key="liu2022common", title="Common Risk Factors in Cryptocurrency",
         authors="Liu, Y., Tsyvinski, A. & Wu, X.", year=2022, venue="Journal of Finance 77(2)",
         url="https://onlinelibrary.wiley.com/doi/10.1111/jofi.13119",
         one_line="A three-factor model (market, size, momentum) prices the crypto cross-section.",
         used_for="Queued — a factor benchmark to check whether any strategy here is just momentum in disguise.",
         tags=["crypto"], status=QUEUED),

    # ── machine learning, frontier ───────────────────────────────────────────
    dict(key="gu2020", title="Empirical Asset Pricing via Machine Learning",
         authors="Gu, S., Kelly, B. & Xiu, D.", year=2020, venue="Review of Financial Studies 33(5)",
         url="https://academic.oup.com/rfs/article/33/5/2223/5758276",
         one_line="Trees and neural nets beat linear models for return prediction, mostly via interactions.",
         used_for="Queued — the argument for gradient boosting once there are enough labelled breakouts to justify it.",
         tags=["machine learning"], status=QUEUED),
    dict(key="zeng2023", title="Are Transformers Effective for Time Series Forecasting?",
         authors="Zeng, A. et al.", year=2023, venue="AAAI 2023",
         url="https://arxiv.org/abs/2205.13504",
         one_line="A one-layer linear model matches or beats transformer forecasters on standard benchmarks.",
         used_for="Queued, and a useful corrective: reach for the simple model first.",
         tags=["machine learning"], status=QUEUED),
    dict(key="nie2023patchtst", title="A Time Series is Worth 64 Words: Long-term Forecasting with Transformers",
         authors="Nie, Y. et al.", year=2023, venue="ICLR 2023",
         url="https://arxiv.org/abs/2211.14730",
         one_line="Patching plus channel independence makes transformers competitive on long-horizon forecasting.",
         used_for="Queued — only relevant once there are years of minute data, which there are not yet.",
         tags=["machine learning"], status=QUEUED),
    dict(key="sirignano2019", title="Universal Features of Price Formation in Financial Markets",
         authors="Sirignano, J. & Cont, R.", year=2019, venue="Quantitative Finance 19(9)",
         url="https://arxiv.org/abs/1803.06917",
         one_line="A single deep model trained across many stocks learns a universal price-formation relationship.",
         used_for="Queued — the case for pooling across coins rather than fitting each one alone.",
         tags=["machine learning", "microstructure"], status=QUEUED),

    # ── adjacent fields, honestly labelled ───────────────────────────────────
    dict(key="orus2019", title="Quantum Computing for Finance: Overview and Prospects",
         authors="Orus, R., Mugel, S. & Lizaso, E.", year=2019, venue="Reviews in Physics 4",
         url="https://arxiv.org/abs/1807.03890",
         one_line="Where quantum algorithms might eventually help finance: optimisation, Monte Carlo, and not much else yet.",
         used_for="Context only. Nothing here needs or benefits from quantum hardware, and claiming otherwise would be noise.",
         tags=["frontier"], status=READ),
    dict(key="preis2013", title="Quantifying Trading Behavior in Financial Markets Using Google Trends",
         authors="Preis, T., Moat, H.S. & Stanley, H.E.", year=2013, venue="Scientific Reports 3",
         url="https://www.nature.com/articles/srep01684",
         one_line="Search-volume changes preceded market moves in-sample; the effect weakened badly out of sample afterwards.",
         used_for="A cautionary example: an attention proxy that looked strong and decayed once published.",
         tags=["behavioural"], status=READ),
]


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS papers (
        key TEXT PRIMARY KEY,
        added_ts REAL NOT NULL,
        payload_json TEXT NOT NULL
    )""")


def seed() -> int:
    ensure_schema()
    now = time.time()
    for p in PAPERS:
        db.execute(
            "INSERT INTO papers(key, added_ts, payload_json) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
            (p["key"], now, json.dumps(p)))
    return len(PAPERS)


def add(paper: dict) -> dict:
    """Append a paper as the library grows. `key` and `title` are required."""
    ensure_schema()
    if not paper.get("key") or not paper.get("title"):
        raise ValueError("a paper needs at least a key and a title")
    paper.setdefault("status", QUEUED)
    paper.setdefault("tags", [])
    db.execute("INSERT OR REPLACE INTO papers(key, added_ts, payload_json) VALUES (?,?,?)",
               (paper["key"], time.time(), json.dumps(paper)))
    return paper


def all_papers(status: str | None = None, tag: str | None = None) -> dict:
    ensure_schema()
    if not db.query_one("SELECT COUNT(*) c FROM papers")["c"]:
        seed()
    rows = [json.loads(r["payload_json"]) | {"added_ts": r["added_ts"]}
            for r in db.query("SELECT * FROM papers")]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    rank = {APPLIED: 0, READ: 1, QUEUED: 2}
    rows.sort(key=lambda r: (rank.get(r.get("status"), 3), -int(r.get("year", 0))))
    tags: dict[str, int] = {}
    for r in rows:
        for t in r.get("tags") or []:
            tags[t] = tags.get(t, 0) + 1
    return {
        "papers": rows,
        "counts": {
            "total": len(rows),
            "applied": sum(1 for r in rows if r.get("status") == APPLIED),
            "read": sum(1 for r in rows if r.get("status") == READ),
            "queued": sum(1 for r in rows if r.get("status") == QUEUED),
        },
        "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
        "rule": ("'applied' means a specific line of code in this project implements it, and "
                 "the entry names the file. 'read' and 'queued' are a reading list with "
                 "reasons, not a claim of expertise."),
    }
