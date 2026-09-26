"""No machine-specific absolute path may be committed.

An assistant that works inside its own sandbox will, given the chance, write that
sandbox's absolute path into a file and commit it. That happened here twice in a
single file. `research/exit_rule_study.py` read the database from
`/sessions/<id>/mnt/<project>/data/...` and wrote its results to
`/sessions/<id>/exitlab/rows.json`. Both were committed, and both were public.

The failure is quiet in the worst way available. The script ran perfectly for
whoever wrote it, because that path existed in their environment. For everybody
else it raises FileNotFoundError against a directory they have never heard of, in
a repository that otherwise presents itself as carefully engineered. Nothing in
the suite inspected string literals, so nothing objected for weeks.

This is the S4 class in `docs/ENGINEERING.md`: correct in the environment that
produced it, broken in the artifact everyone else receives. The evidence lives in
the delivered file rather than in any return value, so the assertion has to read
the file.

The check is deliberately narrow. It bans paths that can only exist on one
machine. It says nothing about `/usr`, `/bin`, `/opt` or `/etc`, which exist
everywhere, nor about `~` and `Path.home()`, which are portable by construction.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "venv", "__pycache__",
             ".pytest_cache", ".ruff_cache", "data", "logs", "secrets", "backups"}

SCAN_SUFFIXES = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".css", ".md",
                 ".html", ".json", ".webmanifest", ".yml", ".yaml", ".txt",
                 ".cfg", ".toml", ".example"}

# The needles are assembled from fragments so that this file does not match its
# own check. That is not paranoia: a test in this suite once fired on the very
# comment explaining what it forbade, and the fix was to stop letting the
# subject's documentation look like the subject.
_SANDBOX = "/ses" "sions/"
_AGENT_HOME = "/ho" "me/cla" "ude"
_AGENT_MOUNT = "/mnt/us" "er-data"
_AGENT_SKILLS = "/mnt/sk" "ills"

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("an agent sandbox root", re.compile(re.escape(_SANDBOX) + r"[A-Za-z0-9_-]{6,}")),
    ("a container home directory", re.compile(re.escape(_AGENT_HOME))),
    ("a container mount point", re.compile(re.escape(_AGENT_MOUNT))),
    ("a container skills mount", re.compile(re.escape(_AGENT_SKILLS))),
    # A named person's home directory. `/Users/you` is the placeholder this repo
    # already uses in examples and in the share-bundle sanitiser, so it stays.
    ("a specific user's home directory",
     re.compile(r"/Users/(?!you\b)[A-Za-z0-9._-]+/")),
]


def _files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
            continue
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        if p.name == Path(__file__).name:          # this file holds the needles
            continue
        out.append(p)
    return out


def test_no_machine_specific_absolute_path_is_committed():
    """The whole point: a path that exists on exactly one machine is a defect in
    every copy but one, and nothing at runtime will tell you so."""
    offenders = []
    for f in _files():
        try:
            text = f.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for what, rx in FORBIDDEN:
                m = rx.search(line)
                if m:
                    rel = f.relative_to(ROOT)
                    offenders.append(f"{rel}:{lineno} contains {what}: {m.group(0)!r}")
    assert not offenders, (
        "these paths exist on one machine only, so every other copy of this "
        "repository is broken at that line:\n  " + "\n  ".join(offenders)
        + "\n\nFix: derive the path from the file's own location "
          "(Path(__file__).resolve().parent...), from a settings property, or "
          "from Path.home(). Never paste an absolute path from your own shell."
    )


def test_the_scan_actually_covers_the_repository():
    """A guard that silently matches nothing is worse than no guard. If the walk
    or the skip-list ever excludes everything, this fails rather than passing
    vacuously -- which is how the original defect survived in the first place."""
    files = _files()
    assert len(files) > 100, f"the scan only reached {len(files)} files; check SKIP_DIRS"
    names = {f.name for f in files}
    for expected in ("config.py", "run.sh", "README.md"):
        assert expected in names, f"{expected} was not scanned"


def test_the_pattern_would_have_caught_the_original_defect():
    """Pin the regexes against the two literal lines that shipped, rebuilt from
    fragments. Without this, a later 'simplification' of the patterns could pass
    every other test while detecting nothing."""
    original_read = _SANDBOX + "rcw-0" + "1abcdef/mnt/project/data/db.sqlite"
    original_write = _SANDBOX + "rcw-0" + "1abcdef/exitlab/rows.json"
    rx = dict(FORBIDDEN)["an agent sandbox root"]
    assert rx.search(original_read), "the read path would no longer be caught"
    assert rx.search(original_write), "the write path would no longer be caught"
    home_rx = dict(FORBIDDEN)["a specific user's home directory"]
    assert home_rx.search("/Users/somebody/projects/x"), "a real home must be caught"
    assert not home_rx.search("/Users/you/projects/x"), "the placeholder must be allowed"
