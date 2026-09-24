"""Central configuration. Every risk limit lives here and nowhere else.

Design rule: it must be impossible to place a real order without an explicit,
typed confirmation string in .env. Defaults are always the safe ones.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"), env_prefix="", extra="ignore"
    )

    # --- service ---
    backend_host: str = Field("127.0.0.1", alias="BACKEND_HOST")
    backend_port: int = Field(8006, alias="BACKEND_PORT")
    log_level: str = Field("INFO", alias="BACKEND_LOG_LEVEL")
    allowed_origins: str = Field(
        "http://127.0.0.1:5180,http://localhost:5180", alias="ALLOWED_ORIGINS"
    )

    # --- storage ---
    db_path: str = Field("./data/tradecrypto.sqlite", alias="TC_DB_PATH")
    # The kill switch is a FILE, so that a dead backend, a stuck event loop or
    # a second process can all still see it. That also means anything that
    # resolves this path writes the operator's real switch. On 2026-09-21
    # 08:37 a unit test did: it built a $150 loss in a private temp database,
    # called pre_trade_check, and the guard wrote data/KILL_SWITCH in the
    # project -- the live desk opened nothing for the rest of the morning
    # on a loss that never happened. The suite now points this at its own
    # temp directory (tests/conftest.py) and refuses to run if it does not.
    kill_switch_path: str = Field("./data/KILL_SWITCH", alias="TC_KILL_SWITCH_FILE")

    # --- execution ---
    execution_mode: str = Field("paper", alias="TC_EXECUTION_MODE")
    live_confirm: str = Field("", alias="TC_LIVE_CONFIRM")
    rh_mcp_url: str = Field("https://agent.robinhood.com/mcp/trading", alias="TC_RH_MCP_URL")
    rh_token_path: str = Field("./secrets/robinhood_api.json", alias="TC_RH_TOKEN_PATH")
    # Robinhood Crypto Trading REST API (docs.robinhood.com/crypto/trading).
    # Either set these, or drop the same two fields into the JSON file above.
    rh_api_key: str = Field("", alias="TC_RH_API_KEY")
    rh_private_key: str = Field("", alias="TC_RH_PRIVATE_KEY")

    # --- data ---
    primary_feed: str = Field("coinbase", alias="TC_PRIMARY_FEED")
    fallback_feed: str = Field("kraken", alias="TC_FALLBACK_FEED")
    feed_timeout_s: float = Field(10.0, alias="TC_FEED_TIMEOUT_S")
    poll_interval_s: float = Field(15.0, alias="TC_POLL_INTERVAL_S")

    # --- risk ---
    account_equity: float = Field(500.0, alias="TC_ACCOUNT_EQUITY")
    max_daily_loss_usd: float = Field(15.0, alias="TC_MAX_DAILY_LOSS_USD")
    max_daily_loss_pct: float = Field(3.0, alias="TC_MAX_DAILY_LOSS_PCT")
    # ── how much goes on a trade, and how many trades exist ──────────────────
    # NONE OF THIS IS A SLOT COUNT. There is no "we take N trades a day" number
    # anywhere in this system, because any such number is a guess dressed up as a
    # rule. What decides the size of a position is free cash and how strong the
    # signal is; what decides HOW MANY positions exist is whether there is still
    # cash left after the ones already open. Two trades, or none, or ten, are all
    # legal outcomes of the same rule, and which one happens is the market's
    # business rather than a setting.
    #
    # The three fields below are runaway backstops. If one of them is ever the
    # reason a trade did not happen, that is a bug report about the sizing, not a
    # number to tune -- and `guards.py` records exactly that when it trips.

    # A single position may not exceed this share of equity. 1.0 means "a bug
    # cannot bet more than the account", which is the only thing this is for. It
    # is a FRACTION, not dollars, so it cannot silently become a position size
    # the way a fixed $400 did.
    max_position_frac: float = Field(1.0, alias="TC_MAX_POSITION_FRAC")
    # 0 disables the cap entirely, which is the default and the intended state.
    # Cash already prevents an eleventh position when ten are open and funded.
    max_concurrent_positions: int = Field(0, alias="TC_MAX_CONCURRENT_POSITIONS")
    # Share of FREE CASH for a signal that only just clears its threshold, used
    # when a strategy has no measured conviction curve yet. Not a slot fraction:
    # a strategy that fires three marginal signals ends up with three positions
    # of shrinking size, and one that fires a single loud signal ends up with one
    # large one. See feedback/sizing.py for the fitted version.
    probe_fraction: float = Field(0.45, alias="TC_PROBE_FRACTION")
    # Runaway backstop only. Deliberately far above anything the cash rule can
    # reach: with a floor at the venue minimum, the account fragments into
    # unfundable slices long before this many orders.
    max_trades_per_day: int = Field(200, alias="TC_MAX_TRADES_PER_DAY")

    @property
    def max_position_usd(self) -> float:
        """The runaway ceiling in dollars, derived rather than typed.

        Kept as a property so every existing call site keeps working, but the
        number now moves with the account instead of being a second, contradictory
        opinion about position size that has to be edited by hand whenever equity
        changes. At the default frac of 1.0 this is simply the account, i.e. it
        binds only on a bug.
        """
        return float(self.account_equity) * float(self.max_position_frac)
    max_drawdown_pct: float = Field(10.0, alias="TC_MAX_DRAWDOWN_PCT")
    kelly_fraction: float = Field(0.25, alias="TC_KELLY_FRACTION")

    # --- statistical gates ---
    min_trades_for_live: int = Field(200, alias="TC_MIN_TRADES_FOR_LIVE")
    min_deflated_sharpe: float = Field(0.0, alias="TC_MIN_DEFLATED_SHARPE")
    max_pbo: float = Field(0.35, alias="TC_MAX_PBO")
    cost_safety_multiplier: float = Field(1.5, alias="TC_COST_SAFETY_MULTIPLIER")
    # PAPER ONLY. Take signals that do not clear the cost hurdle, so their real
    # outcomes can be watched instead of argued about. Every backtest here says
    # they lose; this is how that prediction gets tested with no money at risk.
    # It has no effect whatsoever in live mode -- see engine.tick().
    # ON by default. The operator has asked repeatedly to see the bot actually
    # trade rather than read another table of rejections. Every backtest says
    # these trades lose money; taking them in PAPER is how that prediction gets
    # measured instead of argued about, and it costs nothing to be wrong.
    # Live mode ignores this entirely -- see engine.tick().
    paper_ignore_hurdle: bool = Field(True, alias="TC_PAPER_IGNORE_HURDLE")

    # Start the trading engine when the backend starts. Without this the engine
    # sits at "stopped -- collecting nothing" after every restart and every
    # crash, which is why days passed with zero ticks recorded. Live mode is
    # deliberately excluded: real money never starts itself.
    autostart_engine: bool = Field(True, alias="TC_AUTOSTART_ENGINE")

    # A trade budget is a cap, never a quota. This is the minimum quality (expected
    # edge MINUS the per-coin cost hurdle, in bps) that any trade must clear, even
    # with slots about to expire. 0 means "must at least cover costs".
    budget_min_quality_bps: float = Field(0.0, alias="TC_BUDGET_MIN_QUALITY_BPS")

    # A long-running process should stop ITSELF before the OS kills it. At the
    # ceiling the process exits with code 3, which the supervisor treats as a
    # normal restart. A clean exit is restartable; an OS kill is not.
    # 1500 was never reached: macOS killed the process first, fifteen times.
    # A self-restart checkpoints the WAL on the way out; a SIGKILL does not.
    memory_ceiling_mb: float = Field(1000.0, alias="TC_MEMORY_CEILING_MB")

    @field_validator("kelly_fraction")
    @classmethod
    def _cap_kelly(cls, v: float) -> float:
        # Full Kelly maximises log wealth only if your edge estimate is exact.
        # It never is. Half Kelly is the practical ceiling; we hard-cap there.
        return min(max(v, 0.0), 0.5)

    @field_validator("execution_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"paper", "advisory", "mcp"}:
            raise ValueError(f"TC_EXECUTION_MODE must be paper|advisory|mcp, got {v!r}")
        return v

    # ------------------------------------------------------------------
    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def rh_credentials_file(self) -> Path:
        """Robinhood API credentials, resolved against the PROJECT root.

        The backend runs with its working directory set to backend/, so a
        relative path like "./secrets/robinhood_api.json" means two different
        files depending on who resolves it. That is exactly what happened: the
        setup page wrote the credentials to <project>/secrets/… and the API
        client looked for <project>/backend/secrets/… , found nothing, and
        reported "not configured" with the file sitting right there.

        db_file already existed to solve this for the database. Use it.
        """
        p = Path(self.rh_token_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def live_enabled(self) -> bool:
        """Real money may only move when BOTH conditions hold."""
        return self.execution_mode in {"mcp"} and self.live_confirm.strip() == "I_ACCEPT_REAL_MONEY_RISK"

    @property
    def kill_switch_file(self) -> Path:
        p = Path(self.kill_switch_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    def safety_report(self) -> dict:
        """Human-readable statement of exactly what this process is allowed to do."""
        try:
            from app.core import mode as mode_mod
            active = mode_mod.get_mode()
        except Exception:
            active = self.execution_mode
        return {
            "execution_mode": active,
            "env_default_mode": self.execution_mode,
            "live_enabled": self.live_enabled and active == "mcp",
            "live_confirm_present": bool(self.live_confirm.strip()),
            "kill_switch_engaged": self.kill_switch_file.exists(),
            "account_equity_usd": self.account_equity,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_position_usd": self.max_position_usd,
            "max_trades_per_day": self.max_trades_per_day,
            "kelly_fraction": self.kelly_fraction,
            "explanation": (
                "paper = no real orders, fills simulated against live quotes. "
                "advisory = engine writes order tickets for a human to execute. "
                "mcp = engine places real orders, and only if TC_LIVE_CONFIRM is "
                "exactly 'I_ACCEPT_REAL_MONEY_RISK'."
            ),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
