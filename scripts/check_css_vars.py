#!/usr/bin/env python3
"""Fail if a CSS custom property is used but never defined.

Why: the coin-chart modal was written against --panel, --line, --mut, --fg,
--pos, --neg and --panel2. None of them existed. `background: var(--panel)`
resolves to nothing, so the modal had no background and the page showed through
it; `stroke="var(--pos)"` meant the price line was drawn with no colour at all
and was invisible. 32 usages in CSS and 17 in JSX, silently doing nothing.

No bundler or linter catches this — an undefined custom property is legal CSS
that simply renders as if the declaration were absent. So it needs its own check.

    python3 scripts/check_css_vars.py [frontend/src]
"""
from __future__ import annotations

import os
import re
import sys

USE = re.compile(r"var\((--[A-Za-z0-9_-]+)")
DEF = re.compile(r"^\s*(--[A-Za-z0-9_-]+)\s*:", re.M)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "frontend/src"
    used: dict[str, set[str]] = {}
    defined: set[str] = set()
    n_files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for fn in filenames:
            if not fn.endswith((".css", ".jsx", ".js")):
                continue
            path = os.path.join(dirpath, fn)
            n_files += 1
            text = open(path, encoding="utf-8").read()
            for name in USE.findall(text):
                used.setdefault(name, set()).add(path)
            if fn.endswith(".css"):
                defined |= set(DEF.findall(text))
    missing = {k: v for k, v in used.items() if k not in defined}
    print(f"check_css_vars: {n_files} files, {len(defined)} defined, {len(used)} used")
    if not missing:
        print("  OK - every custom property resolves")
        return 0
    for name, where in sorted(missing.items()):
        files = ", ".join(sorted(os.path.relpath(w) for w in where)[:4])
        print(f"  {name}: used but never defined -> {files}")
    print("\nAn undefined custom property renders as if the declaration were absent: "
          "no background, no colour, no border. Define it or fix the name.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
