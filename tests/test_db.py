"""Datenbank: Schema, Migrationen, Fremdschlüssel, Sicherung und Wiederherstellung."""
import os
import sqlite3
import threading

import pytest

from astrostack import db as dbm
from astrostack.db import schema


def _open(tmp_path, **kw):
    return dbm.open_database(str(tmp_path / "system" / dbm.DB_NAME), **kw)


def _project(d, name="M 31"):
    with d.transaction():
        cur = d.execute("INSERT INTO projects (name, folder, created_at) VALUES (?, ?, ?)", (name, name, dbm.now()))
    return cur.lastrowid


def _file(d, path, size=100):
    cur = d.execute("INSERT INTO files (path, size, mtime_ns, discovered_at) VALUES (?, ?, 1, ?)",
                    (path, size, dbm.now()))
    return cur.lastrowid


def test_new_database_has_current_schema(tmp_path):
    d = _open(tmp_path)
    assert d.schema_version == dbm.SCHEMA_VERSION == len(schema.MIGRATIONS)
    tables = {r[0] for r in d.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("projects", "fields", "setups", "source_folders", "files", "project_files", "frame_metrics",
              "sessions", "file_status", "stacks", "stack_files", "masters", "stack_masters", "assets", "events"):
        assert t in tables, t
    assert d.value("PRAGMA foreign_keys") == 1
    assert d.value("PRAGMA journal_mode") == "wal"
    assert d.value("PRAGMA integrity_check") == "ok"
    assert d.query("PRAGMA foreign_key_check") == []


def test_reopen_is_idempotent(tmp_path):
    d = _open(tmp_path)
    pid = _project(d)
    d.close()
    d = _open(tmp_path)
    assert d.value("SELECT name FROM projects WHERE id = ?", (pid,)) == "M 31"


def test_project_names_unique_case_insensitive(tmp_path):
    d = _open(tmp_path)
    _project(d, "NGC 7000")
    with pytest.raises(sqlite3.IntegrityError):
        _project(d, "ngc 7000")


def test_deleting_project_cascades_but_keeps_shared_files(tmp_path):
    d = _open(tmp_path)
    a, b = _project(d, "A"), _project(d, "B")
    with d.transaction():
        fid = _file(d, "/data/flats/f1.fits")
        for pid in (a, b):
            d.execute("INSERT INTO project_files (project_id, file_id) VALUES (?, ?)", (pid, fid))
        d.execute("INSERT INTO file_status VALUES (?, ?, 'skip', 'auto', 'unscharf', ?)", (a, fid, dbm.now()))
        d.event("project_created", a)
    with d.transaction():
        d.execute("DELETE FROM projects WHERE id = ?", (a,))
    assert d.value("SELECT COUNT(*) FROM files") == 1              # Datei gehört auch zu B
    assert d.value("SELECT COUNT(*) FROM project_files") == 1
    assert d.value("SELECT COUNT(*) FROM file_status") == 0
    assert d.value("SELECT COUNT(*) FROM events") == 0


def test_status_values_are_checked(tmp_path):
    d = _open(tmp_path)
    pid = _project(d)
    fid = _file(d, "/x.fits")
    with pytest.raises(sqlite3.IntegrityError):
        d.execute("INSERT INTO file_status VALUES (?, ?, 'weg', 'auto', '', ?)", (pid, fid, dbm.now()))


def test_transaction_rolls_back_on_error(tmp_path):
    d = _open(tmp_path)
    with pytest.raises(RuntimeError):
        with d.transaction():
            _file(d, "/a.fits")
            with d.transaction():                                   # verschachtelt: läuft in der äußeren
                _file(d, "/b.fits")
            raise RuntimeError("abbrechen")
    assert d.value("SELECT COUNT(*) FROM files") == 0


def test_connection_per_thread(tmp_path):
    d = _open(tmp_path)
    errors = []

    def work(i):
        try:
            with d.transaction():
                _file(d, "/t/%d.fits" % i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert d.value("SELECT COUNT(*) FROM files") == 8
    d.close()


def test_newer_database_is_refused(tmp_path):
    d = _open(tmp_path)
    d.execute("PRAGMA user_version = %d" % (dbm.SCHEMA_VERSION + 1))
    d.close()
    with pytest.raises(dbm.DatabaseTooNew):
        _open(tmp_path)


def test_migration_applies_new_steps_and_backs_up_first(tmp_path, monkeypatch):
    bdir = str(tmp_path / "backup")
    d = _open(tmp_path, backup_dir=bdir)
    _project(d)
    d.close()
    assert dbm.list_backups(bdir) == []                             # neue Datenbank: nichts zu sichern
    extra = "ALTER TABLE projects ADD COLUMN color TEXT;"
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS + [extra])
    monkeypatch.setattr("astrostack.db.store.MIGRATIONS", schema.MIGRATIONS)
    monkeypatch.setattr("astrostack.db.store.SCHEMA_VERSION", len(schema.MIGRATIONS))
    d = _open(tmp_path, backup_dir=bdir)
    assert d.schema_version == len(schema.MIGRATIONS)
    assert "color" in [r[1] for r in d.query("PRAGMA table_info(projects)")]
    backups = dbm.list_backups(bdir)
    assert len(backups) == 1 and backups[0].endswith("-vor-update-v%d.db" % len(schema.MIGRATIONS))
    old = sqlite3.connect(backups[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == len(schema.MIGRATIONS) - 1
    assert old.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    old.close()


def test_failed_migration_leaves_database_untouched(tmp_path, monkeypatch):
    d = _open(tmp_path)
    _project(d)
    d.close()
    broken = "ALTER TABLE projects ADD COLUMN ok TEXT; CREATE TABLE kaputt (;"
    monkeypatch.setattr("astrostack.db.store.MIGRATIONS", schema.MIGRATIONS + [broken])
    monkeypatch.setattr("astrostack.db.store.SCHEMA_VERSION", len(schema.MIGRATIONS) + 1)
    with pytest.raises(sqlite3.Error):
        _open(tmp_path)
    monkeypatch.undo()
    d = _open(tmp_path)
    assert d.schema_version == dbm.SCHEMA_VERSION
    assert "ok" not in [r[1] for r in d.query("PRAGMA table_info(projects)")]


def test_backup_on_start_only_when_changed_and_pruned(tmp_path):
    bdir = str(tmp_path / "backup")
    d = _open(tmp_path, backup_dir=bdir)
    _project(d)
    d.close()
    _open(tmp_path, backup_dir=bdir).close()
    assert len(dbm.list_backups(bdir)) == 1                         # geändert seit „nie gesichert“
    _open(tmp_path, backup_dir=bdir).close()
    assert len(dbm.list_backups(bdir)) == 1                         # unverändert → keine neue Sicherung
    d = _open(tmp_path, backup_dir=bdir)
    for i in range(14):
        dbm.backup(d, bdir, keep=10)
    assert len(dbm.list_backups(bdir)) == 10
    assert all(os.path.basename(p).startswith("b2stacker-") for p in dbm.list_backups(bdir))
    assert not [n for n in os.listdir(bdir) if n.endswith(".tmp")]


def test_backup_ignores_foreign_files(tmp_path):
    bdir = tmp_path / "backup"
    bdir.mkdir()
    (bdir / "meine-notizen.db").write_text("x")
    d = _open(tmp_path)
    for _ in range(3):
        dbm.backup(d, str(bdir), keep=1)
    assert (bdir / "meine-notizen.db").exists()
    assert len(dbm.list_backups(str(bdir))) == 1


def test_restore_backup(tmp_path):
    bdir = str(tmp_path / "backup")
    d = _open(tmp_path)
    _project(d, "Vorher")
    saved = dbm.backup(d, bdir, label="test")
    with d.transaction():
        d.execute("DELETE FROM projects")
    d.close()
    dbm.restore_backup(saved, d.path)
    d = _open(tmp_path)
    assert [r[0] for r in d.query("SELECT name FROM projects")] == ["Vorher"]


def test_restore_refuses_corrupt_backup(tmp_path):
    bad = tmp_path / "b2stacker-20260101-000000.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    d = _open(tmp_path)
    _project(d, "Bleibt")
    d.close()
    with pytest.raises(sqlite3.DatabaseError):
        dbm.restore_backup(str(bad), d.path)
    d = _open(tmp_path)
    assert d.value("SELECT COUNT(*) FROM projects") == 1


def test_backup_label_validated(tmp_path):
    d = _open(tmp_path)
    with pytest.raises(ValueError):
        dbm.backup(d, str(tmp_path / "b"), label="../böse")
