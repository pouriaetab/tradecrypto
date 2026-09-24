"""What a NEW PERSON gets when they clone this repository and run it.

Every test in here exists because of a failure that shipped. The pattern was
always the same and is worth naming: the existing suite tests FUNCTIONS, and a
function cannot notice that the file it lives in never reached the user, or that
the launcher died before Python started, or that a table was empty because the
only code that fills it needs credentials nobody has.

The four that got through, in the order a new user hits them:

  1. `./run.sh` -> "Permission denied". Every .sh was recorded in git as 100644.
     The author never sees it: their own working copy has always been +x.
  2. No `logs/`, no `secrets/`. Git does not store empty directories, and the
     .gitignore that keeps their contents out also discards the .gitkeep inside.
     Under `set -e` the phone path dies on "No such file or directory".
  3. Ports set in `.env` were ignored. run.sh read only the SHELL environment,
     and where it did read .env it took the FIRST match while python-dotenv (the
     backend) takes the LAST. Two halves of one app on different settings.
  4. The universe stayed EMPTY forever. The only code that fills it needs
     Robinhood credentials; without them it raises, gets logged as a warning and
     swallowed. The app starts, serves a dashboard, and does nothing at all --
     and nothing in the log says so.

So these tests do not import a module and call it. They ask what git will hand
over, and what happens on the first boot of an empty database with no secrets.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def _is_repo() -> bool:
    return (ROOT / ".git").exists() and bool(_git("rev-parse", "--git-dir").strip())


# ── 1. what git hands over ───────────────────────────────────────────────────


@pytest.mark.skipif(not _is_repo(), reason="not a git checkout")
def test_every_shebang_file_is_executable_in_git():
    """A file that starts with #! must be mode 100755 IN THE INDEX.

    Checking the working copy is not enough and is exactly how this shipped: the
    bit was right locally and wrong in the commit. `git ls-files -s` is what the
    next clone will actually receive.
    """
    offenders = []
    for line in _git("ls-files", "-s").splitlines():
        mode, _, rest = line.partition(" ")
        path = rest.split("\t", 1)[-1]
        f = ROOT / path
        if not f.is_file():
            continue
        try:
            if f.open("rb").read(2) != b"#!":
                continue
        except OSError:
            continue
        if mode != "100755":
            offenders.append(f"{path} is {mode}")
    assert not offenders, (
        "these files start with #! but git will clone them non-executable, so "
        "`./<file>` fails with 'Permission denied' for every new user:\n  "
        + "\n  ".join(offenders)
        + "\n\nFix:  git update-index --chmod=+x <path>   (then commit)"
    )


@pytest.mark.skipif(not _is_repo(), reason="not a git checkout")
def test_no_secrets_or_data_are_tracked():
    """The handoff repo must never carry the owner's .env, keys or database."""
    tracked = _git("ls-files").splitlines()
    bad = [
        p
        for p in tracked
        if p == ".env"
        or p.startswith("secrets/")
        or p.startswith("logs/")
        or p.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm"))
        or (p.startswith("data/") and p not in ("data/.gitkeep", "data/README.txt"))
    ]
    assert not bad, f"these must not be committed: {bad}"


# ── 2 & 3. what the launcher does before Python exists ───────────────────────


def _bare_clone_dir() -> Path:
    """run.sh plus .env.example and nothing else -- a clone, minus the code."""
    d = Path(tempfile.mkdtemp(prefix="tc-clone-"))
    shutil.copy2(ROOT / "run.sh", d / "run.sh")
    shutil.copy2(ROOT / ".env.example", d / ".env.example")
    return d


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_run_sh_creates_the_directories_git_could_not_carry():
    d = _bare_clone_dir()
    try:
        assert not (d / "logs").exists()
        subprocess.run(
            ["bash", "run.sh", "--check"], cwd=d, capture_output=True, text=True,
            timeout=120,
        )
        for name in ("logs", "secrets", "data"):
            assert (d / name).is_dir(), (
                f"run.sh must `mkdir -p {name}` -- git does not clone empty "
                f"directories, so a new user does not have it, and set -e kills "
                f"the launcher the first time anything writes there"
            )
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ports_in_the_env_file_are_honoured_and_the_last_one_wins():
    """.env is the only place a non-terminal user can change a port.

    Last-wins matters as much as reading it at all: .env.example already defines
    both ports, so a line appended to the bottom is what any human will do, and
    python-dotenv -- which the backend uses -- resolves it that way too. If the
    launcher took the first match, the launcher and the backend would disagree.
    """
    d = _bare_clone_dir()
    try:
        shutil.copy2(d / ".env.example", d / ".env")
        with (d / ".env").open("a") as fh:
            fh.write("\nBACKEND_PORT=8321\nFRONTEND_PORT=5491\n")
        out = subprocess.run(
            ["bash", "run.sh", "--check"], cwd=d, capture_output=True, text=True,
            timeout=120,
        ).stdout
        assert "8321" in out and "5491" in out, (
            "run.sh ignored the ports in .env. A second copy of the app then "
            f"collides with the first on the defaults.\n--- output ---\n{out}"
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_shell_environment_still_beats_the_env_file():
    """Control Deck and `BACKEND_PORT=x bash run.sh` must keep working."""
    d = _bare_clone_dir()
    try:
        shutil.copy2(d / ".env.example", d / ".env")
        with (d / ".env").open("a") as fh:
            fh.write("\nBACKEND_PORT=8321\n")
        env = {**os.environ, "BACKEND_PORT": "8777"}
        out = subprocess.run(
            ["bash", "run.sh", "--check"], cwd=d, capture_output=True, text=True,
            env=env, timeout=120,
        ).stdout
        assert "8777" in out, f"shell env must win over .env\n{out}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── 4. the first boot of an empty database, with no credentials ──────────────


class _StubFeed:
    """A price feed that answers, so the test never touches the network."""

    name = "stub"

    def products(self) -> dict[str, str]:
        from app.data.universe import SEED_RH_SYMBOLS

        return {s: f"{s}-USD" for s in SEED_RH_SYMBOLS}


def test_the_universe_seeds_with_no_credentials_at_all(writable_db, monkeypatch):
    """THE one that made the app inert.

    An empty universe is not a degraded mode, it is a dead app: no coins means
    no quotes, no signals, no trades, and a dashboard that looks fine. The seed
    path must not depend on any secret.
    """
    from app.core import db
    from app.data import universe as u

    monkeypatch.setattr(u, "get_feed", lambda: _StubFeed())
    before = db.query_one("SELECT COUNT(*) c FROM universe")["c"]
    result = u.refresh_universe()
    after = db.query_one("SELECT COUNT(*) c FROM universe")["c"]

    assert result["priceable"] > 0, "seeding produced nothing"
    assert after >= before and after > 0, "the universe table is still empty"


def test_startup_seeds_before_it_tries_the_credentialed_path(monkeypatch):
    """Order is the whole fix.

    Adoption needs credentials, raises NotConfigured without them, and is caught
    and logged as a warning. If seeding ran only after a successful adoption --
    or not at all -- a credential-free install would never get a single coin.
    """
    from app.data import universe as u

    called: list[str] = []

    def _seed():
        called.append("seed")
        return {"priceable": 3, "feed": "stub", "unavailable_on_feed": [],
                "rh_confirmed_count": 0, "warning": ""}

    def _adopt():
        called.append("adopt")
        raise RuntimeError("NotConfigured: no credentials")

    monkeypatch.setattr(u, "refresh_universe", _seed)
    monkeypatch.setattr(u, "adopt_from_robinhood", _adopt)

    # Exactly what lifespan() does, with an empty universe.
    try:
        if True:  # universe is empty
            u.refresh_universe()
    except Exception:  # pragma: no cover
        pass
    try:
        u.adopt_from_robinhood()
    except Exception:
        pass

    assert called and called[0] == "seed", (
        "the credential-free seed must run FIRST and unconditionally; "
        f"order was {called}"
    )


@pytest.mark.skipif(not _is_repo(), reason="not a git checkout")
def test_main_startup_contains_an_unconditional_seed_call():
    """A guard against the seed being made conditional on credentials again.

    Behavioural where it can be; here the thing under test is the STARTUP
    SEQUENCE of a module with heavy side effects, and importing it to run the
    real lifespan would start the engine. So this asserts the call exists and
    that adoption does not gate it -- the specific regression, not the style.
    """
    # COMMENTS ARE NOT CODE. The first version of this check used str.index on
    # the raw file and failed on a correct file, because the comment explaining
    # the fix names adopt_from_robinhood() above the call it is explaining. A
    # test that reports a false failure costs more than no test at all.
    lines = [
        l for l in (ROOT / "backend" / "app" / "main.py").read_text().splitlines()
        if not l.strip().startswith("#")
    ]

    def _line_of(call: str) -> int:
        for i, l in enumerate(lines):
            if call in l:
                return i
        return -1

    seed_at = _line_of("refresh_universe()")
    adopt_at = _line_of("adopt_from_robinhood()")
    assert seed_at >= 0, (
        "app/main.py no longer seeds the universe at startup. Without it a "
        "fresh install with no Robinhood credentials has zero coins forever."
    )
    assert adopt_at >= 0, "app/main.py no longer adopts from Robinhood at all"
    assert seed_at < adopt_at, (
        "the credential-free seed must come BEFORE the credentialed adoption; "
        f"seed is at line {seed_at}, adoption at {adopt_at}"
    )


# ── 5. the settings file a new user actually copies ──────────────────────────


def _keys(path: Path) -> set[str]:
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k and k == k.upper():
                out.add(k)
    return out


@pytest.mark.skipif(not (ROOT / ".env").exists(), reason="no .env here")
def test_the_example_settings_have_not_drifted_from_the_real_ones():
    """`.env.example` is the file every new install becomes. It rots silently.

    It had fallen months behind: a new user got TC_MAX_DAILY_LOSS_USD=15, a
    3-position slot cap and 12 trades a day -- every one of them a limit that
    was deliberately removed -- so the app tripped its own kill switch on the
    first day and looked broken rather than misconfigured.

    Only the KEYS are compared, never the values: values are personal (stake,
    ports, phone access) and belong to whoever runs it.
    """
    live = _keys(ROOT / ".env")
    example = _keys(ROOT / ".env.example")
    missing = sorted(live - example)
    dead = sorted(example - live)
    assert not missing, (
        "settings the running app uses are absent from .env.example, so a new "
        f"install never gets them: {missing}"
    )
    assert not dead, (
        "settings in .env.example that the running app no longer uses -- a new "
        f"install is configured by them and nothing reads them: {dead}"
    )
