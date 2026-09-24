"""The app as one tree: page -> API route -> backend module -> database table,
and job -> module -> table. Read from the SOURCE, so it cannot drift from the
code the way a drawing does.

    "is there a way to connect them as tree branches to see the overall
     structure of this webapp?"

How each edge is found (all static, all cheap, cached for ten minutes):

  page -> endpoint     frontend/src/pages/<Page>.jsx calls `api.<name>(...)`;
                       frontend/src/lib/api.js maps <name> to a request path.
  endpoint -> module   the handler in api/v1/routes.py: modules imported inside
                       the function body, plus module-level `app.*` imports the
                       body actually references.
  module -> table      SQL text in the module (FROM / INTO / UPDATE / JOIN /
                       DELETE FROM <name>) intersected with the real table list
                       from sqlite_master, so a word that merely looks like a
                       table is not one.
  job -> module        core/scheduler.py: each `_job_*` function's imports.

It is a map of what CALLS what, not a promise of what runs: the Risk tab's
liveness card is the reminder that "built" and "running" are different words.
"""
from __future__ import annotations

import ast
import re
import time
from pathlib import Path

from app.config import PROJECT_ROOT
from app.core import db

_cache: dict = {"at": 0.0, "tree": None}
TTL_S = 600.0

_SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|DELETE\s+FROM|TABLE\s+IF\s+NOT\s+EXISTS|TABLE)\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)",
                        re.IGNORECASE)


def _tables() -> set[str]:
    try:
        return {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    except Exception:
        return set()


def _module_tables(root: Path, tables: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in sorted((root / "backend" / "app").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        mod = ".".join(f.relative_to(root / "backend").with_suffix("").parts)
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        hits = sorted({m.group(1) for m in _SQL_TABLE.finditer(text)} & tables)
        if hits:
            out[mod] = hits
    return out


def _module_imports(fn: ast.AST, top_level: dict[str, str]) -> list[str]:
    """Modules a function body reaches: its own `from app.x import y` plus any
    module-level app.* name it references as `name.attr`."""
    mods: set[str] = set()
    used_names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            for alias in node.names:
                cand = f"{node.module}.{alias.name}"
                mods.add(cand if _looks_like_module(cand) else node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    mods.add(alias.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used_names.add(node.value.id)
        elif isinstance(node, ast.Name):
            used_names.add(node.id)
    for name, mod in top_level.items():
        if name in used_names:
            mods.add(mod)
    return sorted(mods)


_MODULE_FILES: set[str] = set()


def _looks_like_module(dotted: str) -> bool:
    return dotted in _MODULE_FILES


def _top_level_imports(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            for alias in node.names:
                cand = f"{node.module}.{alias.name}"
                out[alias.asname or alias.name] = cand if _looks_like_module(cand) else node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    out[alias.asname or alias.name.split(".")[-1]] = alias.name
    return out


def _routes(root: Path) -> list[dict]:
    src = (root / "backend" / "app" / "api" / "v1" / "routes.py").read_text(errors="ignore")
    tree = ast.parse(src)
    top = _top_level_imports(tree)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                out.append({"method": dec.func.attr.upper(), "path": str(dec.args[0].value),
                            "handler": node.name,
                            "doc": (ast.get_docstring(node) or "").split("\n")[0][:140],
                            "modules": [m for m in _module_imports(node, top)
                                        if m not in ("app.core.db", "app.config")]})
    return out


def _jobs(root: Path) -> list[dict]:
    src = (root / "backend" / "app" / "core" / "scheduler.py").read_text(errors="ignore")
    tree = ast.parse(src)
    top = _top_level_imports(tree)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    out = []
    # JOBS = { "name": {"fn": _job_x, "interval": ..., "what": "..."} }
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if targets and any(isinstance(t, ast.Name) and t.id == "JOBS" for t in targets) \
                and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if not (isinstance(k, ast.Constant) and isinstance(v, ast.Dict)):
                    continue
                spec = {kk.value: vv for kk, vv in zip(v.keys, v.values) if isinstance(kk, ast.Constant)}
                fn = spec.get("fn")
                fname = fn.id if isinstance(fn, ast.Name) else None
                what = spec.get("what")
                interval = spec.get("interval")
                out.append({"job": k.value, "handler": fname,
                            "what": (what.value if isinstance(what, ast.Constant) else
                                     ast.unparse(what) if what is not None else ""),
                            "interval": ast.unparse(interval) if interval is not None else "",
                            "modules": [m for m in _module_imports(fns[fname], top)
                                        if m not in ("app.core.db", "app.config")]
                            if fname in fns else []})
    return out


def _api_map(root: Path) -> dict[str, str]:
    """api.js: name -> request path (template literals reduced to their static prefix)."""
    src = (root / "frontend" / "src" / "lib" / "api.js").read_text(errors="ignore")
    out: dict[str, str] = {}
    for m in re.finditer(r"^\s*(\w+):\s*\([^)]*\)\s*=>\s*(.*?)(?=^\s*\w+:\s*\(|\Z)", src, re.M | re.S):
        name, body = m.group(1), m.group(2)
        r = re.search(r"req\((['`\"])(.*?)\1", body)
        if not r:
            continue
        path = re.split(r"\$\{|\?", r.group(2))[0]
        out[name] = path.rstrip("/") or "/"
    return out


def _pages(root: Path, api_map: dict[str, str]) -> list[dict]:
    out = []
    for f in sorted((root / "frontend" / "src" / "pages").glob("*.jsx")):
        text = f.read_text(errors="ignore")
        names = sorted(set(re.findall(r"\bapi\.(\w+)\s*\(", text)))
        out.append({"page": f.stem, "calls": [{"name": n, "path": api_map.get(n)} for n in names]})
    return out


def _match_route(path: str | None, routes: list[dict]) -> dict | None:
    if not path:
        return None
    best = None
    for r in routes:
        rp = r["path"]
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", rp) + "$"
        if re.match(pattern, path):
            return r
        if path.startswith(rp.split("{")[0].rstrip("/")) and (best is None or len(rp) > len(best["path"])):
            best = r
    return best


def tree(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["tree"] is not None and now - _cache["at"] < TTL_S:
        return _cache["tree"]
    root = Path(PROJECT_ROOT)
    global _MODULE_FILES
    _MODULE_FILES = {".".join(p.relative_to(root / "backend").with_suffix("").parts)
                     for p in (root / "backend" / "app").rglob("*.py") if "__pycache__" not in p.parts}
    tables = _tables()
    mod_tables = _module_tables(root, tables)
    routes = _routes(root)
    for r in routes:
        r["tables"] = sorted({t for m in r["modules"] for t in mod_tables.get(m, [])})
    jobs = _jobs(root)
    for j in jobs:
        j["tables"] = sorted({t for m in j["modules"] for t in mod_tables.get(m, [])})
    api_map = _api_map(root)
    pages = _pages(root, api_map)
    for p in pages:
        eps = []
        for c in p["calls"]:
            r = _match_route(c["path"], routes)
            eps.append({"call": c["name"], "path": c["path"], "route": r["path"] if r else None,
                        "method": r["method"] if r else None, "handler": r["handler"] if r else None,
                        "modules": r["modules"] if r else [], "tables": r["tables"] if r else []})
        p["endpoints"] = eps
        p["tables"] = sorted({t for e in eps for t in e["tables"]})
        del p["calls"]
    # tables -> who touches them
    touched: dict[str, dict] = {t: {"modules": [], "pages": [], "jobs": []} for t in sorted(tables)}
    for m, ts in mod_tables.items():
        for t in ts:
            touched[t]["modules"].append(m)
    for p in pages:
        for t in p["tables"]:
            touched[t]["pages"].append(p["page"])
    for j in jobs:
        for t in j["tables"]:
            touched[t]["jobs"].append(j["job"])
    out = {"generated_at": now, "pages": pages, "jobs": jobs, "routes": routes,
           "tables": touched, "modules": sorted(_MODULE_FILES),
           "counts": {"pages": len(pages), "routes": len(routes), "jobs": len(jobs),
                      "modules": len(_MODULE_FILES), "tables": len(tables)},
           "note": ("Static map of what calls what, read from the source: page -> api.js -> "
                    "route -> modules imported by the handler -> tables named in those modules' "
                    "SQL. Module-level imports count only when the handler references them.")}
    _cache.update(at=now, tree=out)
    return out
