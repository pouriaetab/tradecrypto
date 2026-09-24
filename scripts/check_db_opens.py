#!/usr/bin/env python3
"""Who opens a SQLite database, how, and which file?

This replaces a grep. Grep cannot tell a `sqlite3.connect(...)` CALL from the
same text quoted inside an error message, and it cannot tell which FILE is being
opened -- so the grep version flagged two explanatory strings in dbguard.py and
two opens of `tradecrypto.recovered.sqlite`, a file nothing else holds. A gate
that cries wolf gets switched off (CHECKLIST 2.14 / SAFEGUARDS 5.1), so this
walks the AST instead and judges by the target.

Rules enforced
  1. Nothing outside the app's own database layer opens the LIVE file read-write.
     SQLite's POSIX locks do not cross a mount; two writers destroyed it once.
  2. Nothing outside the owning process reads the LIVE file with `mode=ro` alone.
     A WAL reader still maps and WRITES the -shm index. Use `immutable=1`.

Exempt, on purpose:
  core/db.py, core/dbrecover.py  -- the database layer itself
  core/housekeeping.py           -- the in-process snapshot MUST see the WAL, so
                                    mode=ro is correct there and immutable=1
                                    would silently capture stale data.

Exit 0 clean, 1 with findings.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

EXEMPT = {"db.py", "dbrecover.py", "housekeeping.py"}
# A target whose name says it is not the live file.
NOT_LIVE = ("recovered", "backup", "quarantine", "snapshot", "ledger-",
            "tmp", "temp", "copy", "candidate", "cand")


def _uri_text(node: ast.AST) -> str | None:
    """The connect() target as text, with interpolations as {name}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                out.append("{" + ast.unparse(v.value) + "}")
        return "".join(out)
    return "{" + ast.unparse(node) + "}"


def _looks_live(uri: str, assigns: dict[str, str]) -> bool:
    """Is this plausibly the live database rather than a copy?"""
    probe = uri
    for name, rhs in assigns.items():
        if "{" + name + "}" in uri or "{" + name + "." in uri:
            probe += " " + rhs
    return not any(w in probe.lower() for w in NOT_LIVE)


import re

_HEREDOC = re.compile(r"<<\s*'?(PY\w*)'?\s*\n(.*?)\n\1\s*$", re.S | re.M)


def _python_sources(root: Path):
    """Every Python source under root: .py files, and the Python heredocs
    inside shell scripts. 2026-09-21: scripts/facts.sh opened the live file
    `mode=ro` from a `python3 - <<'PY'` block. This checker walked only *.py,
    so the open that killed the backend (SIGBUS, 09:08) was invisible to the
    gate that exists to catch exactly that."""
    for py in sorted(root.rglob("*.py")):
        if ".venv" in py.parts or "site-packages" in py.parts or py.name in EXEMPT:
            continue
        yield str(py), 0, py.read_text()
    for sh in sorted(root.rglob("*.sh")):
        text = sh.read_text()
        for m in _HEREDOC.finditer(text):
            line0 = text[:m.start(2)].count("\n")
            yield f"{sh} (heredoc)", line0, m.group(2)


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    findings = []
    for name, base, src in _python_sources(root):
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            findings.append((name, base + (e.lineno or 0), "unparseable", str(e)))
            continue
        py = name

        # Every simple assignment in the file, so `cand = ...recovered...` is
        # visible when we judge `connect(f"file:{cand}?...")`.
        assigns: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                try:
                    assigns[n.targets[0].id] = ast.unparse(n.value)
                except Exception:
                    pass

        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "connect"):
                continue
            mod = getattr(fn.value, "id", "")
            if not mod.endswith("sq") and mod not in ("sqlite3", "_sq", "sq"):
                continue
            if not n.args:
                continue
            uri = _uri_text(n.args[0]) or ""
            if not _looks_live(uri, assigns):
                continue
            low = uri.lower()
            if "immutable=1" in low:
                continue
            if "mode=ro" in low:
                findings.append((str(py), base + n.lineno, "mode=ro without immutable=1", uri))
            else:
                findings.append((str(py), base + n.lineno, "read-write open", uri))
    return findings


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]] or [Path("backend/app"), Path("scripts")]
    findings = []
    for r in roots:
        if r.exists():
            findings += scan(r)
    for path, line, why, uri in findings:
        print(f"{path}:{line}: {why}  ->  {uri}")
    if findings:
        print(f"{len(findings)} finding(s): use immutable=1, or work on a copy")
        return 1
    print("every database open outside the database layer is inert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
