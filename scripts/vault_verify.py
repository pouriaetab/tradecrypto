#!/usr/bin/env python3
"""Check the vault, using nothing from this project.

Deliberately standalone: stock python3, no imports from `app.*`, no database, no
config. If every other thing here were deleted tomorrow, this file plus the
vault folder would still be enough to prove what was traded -- which is the only
reason the vault is worth having.

    python3 scripts/vault_verify.py                 # verify the whole vault
    python3 scripts/vault_verify.py --dir ~/some/other/vault
    python3 scripts/vault_verify.py --show trade:37 # print one record

Exit 0 clean, 1 if anything does not verify.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

GENESIS = "0" * 64


def vault_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("TC_VAULT_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / "tradecrypto-vault"


def verify_file(p: Path) -> dict:
    """Walk one day's chain. A truncated tail still verifies up to the cut."""
    prev, n, bad, partial = GENESIS, 0, None, None
    with p.open(encoding="utf-8") as fh:
        for i, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                # A crash mid-write leaves a partial LAST line, and the whole
                # point of an fsynced append-only file is that this costs you
                # the interrupted record and NOTHING ELSE. So: unreadable final
                # line = expected, reported, not a failure. Unreadable line with
                # more after it = someone has been in the file.
                rest = [x for x in fh if x.strip()]
                if rest:
                    bad = f"line {i} is not valid JSON, and {len(rest)} line(s) follow it"
                else:
                    partial = i
                break
            body = dict(rec)
            got_prev, got_h = body.pop("prev", None), body.pop("h", None)
            if got_prev != prev:
                bad = (f"line {i} ({rec.get('key')}) says the line before it hashed to "
                       f"{str(got_prev)[:12]}…, but it hashed to {prev[:12]}… — "
                       f"a line was changed or removed at or before here")
                break
            want = hashlib.sha256(
                (prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()
            if want != got_h:
                bad = f"line {i} ({rec.get('key')}) has been edited since it was written"
                break
            prev, n = got_h, n + 1
    return {"file": p.name, "records": n, "ok": bad is None, "why": bad,
            "partial_line": partial}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--show", help="print the record with this key")
    args = ap.parse_args()

    root = vault_dir(args.dir)
    recs = root / "records"
    if not recs.is_dir():
        print(f"no vault at {root}")
        print("  the app creates it on its next order, trade or reconcile pass.")
        return 1

    files = sorted(recs.glob("*.ndjson"))
    if args.show:
        for f in files:
            with f.open(encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    if rec.get("key") == args.show:
                        print(json.dumps(rec, indent=2))
                        return 0
        print(f"no record with key {args.show}")
        return 1

    total, failures, keys, partial_files = 0, [], set(), 0
    print(f"vault: {root}")
    for f in files:
        r = verify_file(f)
        total += r["records"]
        sealed = "sealed" if not (f.stat().st_mode & 0o222) else "open"
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  {mark} {r['file']}  {r['records']:5} record(s)  {sealed}")
        if not r["ok"]:
            print(f"       {r['why']}")
            failures.append(r)
        elif r["partial_line"]:
            partial_files += 1
            print(f"       note: line {r['partial_line']} was still being written when "
                  f"something stopped. Everything before it verifies; that one "
                  f"record did not complete. This is what a crash looks like, "
                  f"not tampering.")
        with f.open(encoding="utf-8") as fh:
            for raw in fh:
                try:
                    keys.add(json.loads(raw)["key"])
                except Exception:
                    pass

    idx = root / "index.txt"
    idx_keys = set()
    if idx.exists():
        idx_keys = {ln.strip() for ln in idx.read_text().splitlines() if ln.strip()}
    missing_from_files = idx_keys - keys
    print(f"\n  {total} record(s), {len(keys)} distinct key(s), "
          f"{len(idx_keys)} in the index")
    if missing_from_files:
        # An interrupted append is allowed to cost exactly its own record, so a
        # single missing key alongside a single partial line is that, not a
        # removal. More than that is a removal and is reported as one.
        expected = partial_files
        if len(missing_from_files) <= expected:
            print(f"  note: {len(missing_from_files)} key(s) in the index have no "
                  f"complete record — the append(s) that were interrupted above.")
        else:
            print(f"  FAIL {len(missing_from_files)} key(s) are in the index but not "
                  f"in any day file — records have been removed")
            for k in sorted(missing_from_files)[:10]:
                print(f"       {k}")
            failures.append({"file": "index.txt"})

    if failures:
        print("\n  THE VAULT DOES NOT VERIFY. Do not delete anything; the broken "
              "file is evidence.")
        return 1
    print("\n  every record verifies, and nothing has been removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
