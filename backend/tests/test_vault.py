"""The vault: a write-once copy of every order and trade, outside everything else.

The operator's requirement was "never miss any of the orders data, i need this to
be 100%". 100% is not a promise you make, it is two mechanisms: a synchronous
fsynced write, and a reconciler that proves the write happened.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _order(db, symbol: str, n=[0]):
    """One order row, with every NOT NULL column the schema actually requires."""
    n[0] += 1
    db.execute(
        "INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
        "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"test-{symbol}-{n[0]}", "day_climb", symbol, "buy", "open", 10.0,
         100.0, "paper", "filled", 1789600000))

@pytest.fixture
def vault_at(tmp_path, monkeypatch):
    monkeypatch.setenv("TC_VAULT_DIR", str(tmp_path / "vault"))
    from app.core import vault
    return vault


class TestItIsActuallyIndependent:
    """Independence is the whole feature. Assert it, don't assume it."""

    def test_it_lives_outside_the_project(self, vault_at):
        vault_at.record("order", "order:1", {"symbol": "ARB"})
        st = vault_at.status()
        assert st["inside_project"] is False, \
            f"the vault is inside the project at {st['dir']} — one rm takes both copies"

    def test_the_default_location_is_outside_the_project(self, monkeypatch):
        monkeypatch.delenv("TC_VAULT_DIR", raising=False)
        from app.core import vault
        d = str(vault.vault_dir())
        assert "tradecrypto-vault" in d
        assert "/market/tradecrypto/" not in d

    def test_the_write_path_imports_nothing_from_this_app(self):
        """A bug anywhere in app.* must not be able to break the one write that
        must not break. `reconcile()` may import db; `record()` may not."""
        import inspect
        from app.core import vault
        for fn in (vault.record, vault._tail_hash, vault.known_keys):
            src = inspect.getsource(fn)
            assert "from app." not in src and "import app" not in src, \
                f"{fn.__name__} reaches back into the app"

    def test_it_is_not_a_database(self, vault_at):
        vault_at.record("order", "order:1", {"symbol": "ARB"})
        f = next((vault_at.vault_dir() / "records").glob("*.ndjson"))
        first = f.read_bytes()[:16]
        assert not first.startswith(b"SQLite format"), "the vault became a database"
        json.loads(f.read_text().splitlines()[0])      # plain, readable JSON


class TestNothingIsMissed:

    def test_a_record_lands_and_is_indexed(self, vault_at):
        assert vault_at.record("order", "order:7", {"symbol": "UNI"}) is True
        assert "order:7" in vault_at.known_keys()
        assert vault_at.status()["records"] == 1

    def test_it_fsyncs_every_record(self, vault_at, monkeypatch):
        """Written is not the same as survives-the-power-going-out.

        Without fsync the record sits in the OS page cache and a hard power loss
        takes it, which is exactly the class of event the vault exists for. The
        first re-break of this file removed the fsync and every other test
        stayed green -- so this one exists.
        """
        import app.core.vault as v
        calls = []
        real = v.os.fsync
        monkeypatch.setattr(v.os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
        assert v.record("order", "order:fsync", {"x": 1}) is True
        assert len(calls) >= 2, \
            f"fsync called {len(calls)} time(s) — the record and its index must both be durable"

    def test_it_never_raises_even_when_the_vault_cannot_be_written(self, tmp_path, monkeypatch):
        """A vault that can stop a trade has made things worse, not better."""
        monkeypatch.setenv("TC_VAULT_DIR", str(tmp_path / "nope"))
        from app.core import vault
        monkeypatch.setattr(vault, "_records_dir",
                            lambda: (_ for _ in ()).throw(OSError("read-only")))
        assert vault.record("order", "order:1", {}) is False      # reported, not raised

    def test_reconcile_appends_what_the_database_has_and_the_vault_does_not(
            self, writable_db, vault_at):
        from app.core import db
        _order(db, "ARB")
        out = vault_at.reconcile()
        assert out["added"] == 1, out
        assert any(k.startswith("order:") for k in vault_at.known_keys())

    def test_reconcile_is_idempotent(self, writable_db, vault_at):
        from app.core import db
        _order(db, "ARB")
        assert vault_at.reconcile()["added"] == 1
        assert vault_at.reconcile()["added"] == 0

    def test_reconcile_catches_a_write_that_failed(self, writable_db, vault_at):
        """The whole reason reconcile exists: a synchronous write CAN fail.

        Restored by hand, not with monkeypatch.undo(): undo() reverts EVERY
        patch on this function including the fixture's TC_VAULT_DIR, which sent
        the rest of the test at the real vault. It passed alone and failed in
        the suite -- an order-dependent test is a test that is lying to you
        about something, and here it was lying about where it was writing.
        """
        from app.core import db, vault
        real = vault.record
        vault.record = lambda *a, **k: False          # the write "fails"
        try:
            _order(db, "ZEC")
        finally:
            vault.record = real
        assert vault_at.reconcile()["added"] == 1


class TestItCannotBeQuietlyEdited:

    def _verify(self, d) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[2] / "scripts" / "vault_verify.py"
        return subprocess.run([sys.executable, str(script), "--dir", str(d)],
                              capture_output=True, text=True, timeout=60)

    def test_a_clean_vault_verifies(self, vault_at):
        for i in range(3):
            vault_at.record("order", f"order:{i}", {"i": i})
        r = self._verify(vault_at.vault_dir())
        assert r.returncode == 0, r.stdout + r.stderr

    def test_editing_a_record_is_detected(self, vault_at):
        for i in range(3):
            vault_at.record("order", f"order:{i}", {"qty": i})
        f = next((vault_at.vault_dir() / "records").glob("*.ndjson"))
        lines = f.read_text().splitlines()
        lines[1] = lines[1].replace('"qty": 1', '"qty": 999')
        f.write_text("\n".join(lines) + "\n")
        r = self._verify(vault_at.vault_dir())
        assert r.returncode == 1
        assert "edited" in r.stdout

    def test_removing_a_record_is_detected(self, vault_at):
        for i in range(3):
            vault_at.record("order", f"order:{i}", {"i": i})
        f = next((vault_at.vault_dir() / "records").glob("*.ndjson"))
        lines = f.read_text().splitlines()
        del lines[1]
        f.write_text("\n".join(lines) + "\n")
        r = self._verify(vault_at.vault_dir())
        assert r.returncode == 1
        assert "removed" in r.stdout

    def test_a_crash_mid_write_is_not_called_tampering(self, vault_at):
        """An interrupted append costs its own record and nothing else."""
        for i in range(3):
            vault_at.record("order", f"order:{i}", {"i": i})
        f = next((vault_at.vault_dir() / "records").glob("*.ndjson"))
        f.write_text(f.read_text()[:-40])
        r = self._verify(vault_at.vault_dir())
        assert r.returncode == 0, r.stdout
        assert "still being written" in r.stdout

    def test_the_verifier_does_not_import_this_project(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "vault_verify.py"
        src = script.read_text()
        assert "from app." not in src and "import app" not in src, \
            "the standalone verifier depends on the app it is meant to outlive"


class TestItIsWiredIn:

    def _src(self, rel: str) -> str:
        return (Path(__file__).resolve().parents[1] / rel).read_text()

    def test_every_order_goes_to_the_vault(self):
        assert 'vault.record("order"' in self._src("app/execution/broker.py")

    def test_every_closed_trade_goes_to_the_vault(self):
        assert 'vault.record("trade"' in self._src("app/execution/engine.py")

    def test_boot_reconciles(self):
        assert "vault.reconcile()" in self._src("app/main.py") or \
               "_vault.reconcile()" in self._src("app/main.py")

    def test_a_scheduled_job_reconciles_too(self):
        """Boot alone would mean a desk that never restarts never re-checks."""
        s = self._src("app/core/scheduler.py")
        assert '"vault"' in s and "_job_vault" in s

    def test_the_mirror_never_force_pushes(self):
        sh = (Path(__file__).resolve().parents[2] / "scripts" / "vault_mirror.sh").read_text()
        body = "\n".join(l for l in sh.splitlines() if not l.strip().startswith("#"))
        assert "--force" not in body and "-f origin" not in body, \
            "a force push can erase the write-once store it is meant to protect"
