#!/usr/bin/env python3
"""Capture every GET endpoint's real `data` as {api method name: payload}.

Feeds frontend/scripts/render-with-data.mjs. Parses the method->path map out of
frontend/src/lib/api.js rather than keeping a second list, so a route added to
the app is captured without anyone remembering to add it here.

Runs the app in-process against whatever TC_DB_PATH points at -- normally the
newest ledger snapshot, never the live file (CLAUDE.md rule 5).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_JS = ROOT / "frontend" / "src" / "lib" / "api.js"

# `name: () => req('/path')` and `name: (a = 'x') => req(\`/path?q=${a}\`)`
PAT = re.compile(r"^\s*(\w+):\s*\(([^)]*)\)\s*=>\s*req\(\s*[`']([^`']+)[`']", re.M)


def routes() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, args, path in PAT.findall(API_JS.read_text()):
        if "${" in path:                      # interpolated -> needs an argument
            if "=" not in args:               # no default to fall back on; skip
                continue
            default = args.split("=", 1)[1].strip().strip("'\"")
            path = re.sub(r"\$\{[^}]+\}", default, path)
        out[name] = path
    return out


def main() -> int:
    os.environ.setdefault("TC_DB_READONLY", "1")
    sys.path.insert(0, str(ROOT / "backend"))
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app, client=("127.0.0.1", 50000))
    blob, failed = {}, []
    for name, path in sorted(routes().items()):
        url = "/api/v1" + path
        try:
            r = c.get(url, timeout=60)
        except Exception as exc:
            failed.append(f"{name} {type(exc).__name__}")
            blob[name] = None
            continue
        if r.status_code == 200:
            try:
                blob[name] = r.json().get("data")
            except Exception:
                blob[name] = None
        else:
            # A page must survive an endpoint that is not answering, so record
            # null rather than dropping the key.
            failed.append(f"{name} {r.status_code}")
            blob[name] = None
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tc_payloads.json")
    dest.write_text(json.dumps(blob))
    print(f"captured {len(blob)} endpoints -> {dest}")
    if failed:
        print("  not 200 (captured as null): " + ", ".join(failed[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
