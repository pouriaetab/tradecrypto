#!/usr/bin/env python3
"""Fail if a JSX component is used without being imported or declared.

Why this exists: Journal.jsx rendered a CoinChart element and never imported it.
Because the guard variable started null, the bad reference was never evaluated,
so the page loaded fine and every build passed. The first click on a symbol threw
ReferenceError inside render, React 18 unmounted the whole tree, and the app went
blank with no navigation left. A bundler cannot catch this -- an undefined
identifier is legal JavaScript until it runs -- so it needs its own check.

Usage:  python3 scripts/check_jsx_imports.py [frontend/src]
Exit 1 if anything is found.
"""
from __future__ import annotations

import collections
import os
import re
import sys

HTML = set("""a abbr address area article aside audio b base bdi bdo blockquote body br button
canvas caption cite code col colgroup data datalist dd del details dfn dialog div dl dt em embed
fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img
input ins kbd label legend li link main map mark menu meta meter nav noscript object ol optgroup
option output p param picture pre progress q rp rt ruby s samp script section select slot small
source span strong style sub summary sup table tbody td template textarea tfoot th thead time
title tr track u ul var video wbr svg path circle rect line g text defs linearGradient stop
polyline polygon ellipse tspan clipPath use symbol foreignObject mask pattern filter""".split())

BLOCK = re.compile(r"/\*.*?\*/", re.S)
LINE = re.compile(r"^\s*//.*$", re.M)


def scope_of(src: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r"import\s+(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?\s*from", src):
        if m.group(1):
            names.add(m.group(1))
        if m.group(2):
            for part in m.group(2).split(","):
                part = part.strip()
                if part:
                    names.add(part.split(" as ")[-1].strip())
    for m in re.finditer(r"(?:function|class)\s+([A-Z][\w$]*)", src):
        names.add(m.group(1))
    for m in re.finditer(r"(?:const|let|var)\s+([A-Z][\w$]*)\s*=", src):
        names.add(m.group(1))
    for m in re.finditer(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", src):
        for part in m.group(1).split(","):
            p = part.strip().split(":")[-1].strip()
            if p:
                names.add(p)
    return names


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "frontend/src"
    bad: dict[str, set[str]] = collections.defaultdict(set)
    n_files = 0
    for dirpath, _, names in os.walk(root):
        if "node_modules" in dirpath:
            continue
        for name in names:
            if not name.endswith((".jsx", ".js")):
                continue
            path = os.path.join(dirpath, name)
            n_files += 1
            raw = open(path, encoding="utf-8").read()
            src = LINE.sub("", BLOCK.sub("", raw))
            scope = scope_of(src)
            for m in re.finditer(r"<([A-Z][\w$.]*)", src):
                tag = m.group(1).split(".")[0]
                if tag not in HTML and tag not in scope:
                    bad[path].add(tag)
    print(f"check_jsx_imports: scanned {n_files} files under {root}")
    if not bad:
        print("  OK - every component used is imported or declared")
        return 0
    for path, tags in sorted(bad.items()):
        print(f"  {path}: used but never imported/declared -> {', '.join(sorted(tags))}")
    print("\nThis is the blank-page class of bug. Fix the imports before shipping.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
