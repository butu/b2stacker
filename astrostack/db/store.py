"""Zugriff auf die SQLite-Datenbank: öffnen, migrieren, sichern, wiederherstellen.

Eine Verbindung je Thread (SQLite-Verbindungen sind nicht threadsicher); WAL-Modus, damit die Oberfläche
lesen kann, während ein Hintergrund-Auftrag schreibt. Fremdschlüssel sind immer eingeschaltet.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re
import sqlite3
import threading
from typing import Any, Iterator

from .schema import MIGRATIONS, SCHEMA_VERSION

DB_NAME = "b2stacker.db"
BACKUP_KEEP = 10
_BACKUP_RE = re.compile(r"^b2stacker-(\d{8}-\d{6}(?:-\d+)?)(?:-[a-z0-9-]+)?\.db$")


class DatabaseTooNew(RuntimeError):
    """Die Datenbank stammt von einer neueren b2stacker-Version; diese Version darf sie nicht anfassen."""


def now() -> str:
    """Aktuelle Zeit als ISO-8601 in UTC, sekundengenau (Format aller Zeitstempel in der Datenbank)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    return con


class Database:
    """Eine geöffnete, migrierte b2stacker-Datenbank."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ Verbindungen
    @property
    def con(self) -> sqlite3.Connection:
        c = getattr(self._local, "con", None)
        if c is None:
            c = _connect(self.path)
            self._local.con = c
            with self._lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()

    # ------------------------------------------------------------ Abfragen
    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.con.execute(sql, params)

    def executemany(self, sql: str, rows) -> sqlite3.Cursor:
        return self.con.executemany(sql, rows)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.con.execute(sql, params).fetchone()

    def value(self, sql: str, params: Any = (), default: Any = None) -> Any:
        row = self.con.execute(sql, params).fetchone()
        return default if row is None else row[0]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Schreib-Transaktion; verschachtelte Aufrufe laufen in der äußeren mit."""
        c = self.con
        if c.in_transaction:
            yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    @property
    def schema_version(self) -> int:
        return int(self.value("PRAGMA user_version", default=0))

    def event(self, kind: str, project_id: int | None = None, data_json: str = "{}") -> int:
        """Ein Ereignis für die History festhalten."""
        cur = self.execute("INSERT INTO events (at, project_id, kind, data_json) VALUES (?, ?, ?, ?)",
                           (now(), project_id, kind, data_json))
        return int(cur.lastrowid)


# ---------------------------------------------------------------- Öffnen und Migrieren

def _migrate(con: sqlite3.Connection) -> list[int]:
    """Fehlende Migrationen anwenden, jede in einer eigenen Transaktion. → angewendete Versionen"""
    current = int(con.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise DatabaseTooNew(
            "Die Datenbank hat Schema-Version %d, diese b2stacker-Version kennt nur bis %d. "
            "Bitte b2stacker aktualisieren." % (current, SCHEMA_VERSION))
    done = []
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        # executescript führt vorher ein COMMIT aus; BEGIN … COMMIT macht die Migration atomar.
        try:
            con.executescript("BEGIN IMMEDIATE;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (script, version))
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        done.append(version)
    return done


def open_database(path: str, backup_dir: str | None = None, keep: int = BACKUP_KEEP) -> Database:
    """Datenbank öffnen (bei Bedarf anlegen) und auf den aktuellen Stand migrieren.

    Mit ``backup_dir`` wird eine vorhandene Datenbank vorher gesichert: immer vor einer Migration,
    sonst nur, wenn sie sich seit der letzten Sicherung geändert hat.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    existed = os.path.exists(path) and os.path.getsize(path) > 0
    con = _connect(path)
    try:
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":          # z. B. Netzlaufwerk ohne Shared Memory
            con.execute("PRAGMA journal_mode = DELETE")
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DatabaseTooNew(
                "Die Datenbank hat Schema-Version %d, diese b2stacker-Version kennt nur bis %d. "
                "Bitte b2stacker aktualisieren." % (version, SCHEMA_VERSION))
        if existed and backup_dir:
            if version < SCHEMA_VERSION:
                _backup_con(con, backup_dir, keep, label="vor-update-v%d" % SCHEMA_VERSION)
            else:
                _backup_con(con, backup_dir, keep, skip_if_unchanged=True)
        _migrate(con)
    finally:
        con.close()
    return Database(path)


# ---------------------------------------------------------------- Sicherung

def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def list_backups(backup_dir: str) -> list[str]:
    """Vorhandene Sicherungen, neueste zuerst (volle Pfade)."""
    try:
        names = [n for n in os.listdir(backup_dir) if _BACKUP_RE.match(n)]
    except OSError:
        return []
    names.sort(key=lambda n: (_BACKUP_RE.match(n).group(1), n), reverse=True)
    return [os.path.join(backup_dir, n) for n in names]


def _same_content(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(1 << 20), fb.read(1 << 20)
            if ca != cb:
                return False
            if not ca:
                return True


def _backup_con(con: sqlite3.Connection, backup_dir: str, keep: int, label: str = "",
                skip_if_unchanged: bool = False) -> str | None:
    os.makedirs(backup_dir, exist_ok=True)
    previous = list_backups(backup_dir)
    base = "b2stacker-" + _stamp()
    name = base + ("-" + label if label else "") + ".db"
    n = 1
    while os.path.exists(os.path.join(backup_dir, name)):     # zwei Sicherungen in derselben Sekunde
        n += 1
        name = "%s-%d%s.db" % (base, n, "-" + label if label else "")
    dest = os.path.join(backup_dir, name)
    tmp = dest + ".tmp"
    out = sqlite3.connect(tmp)
    try:
        con.backup(out)
    finally:
        out.close()
    if skip_if_unchanged and previous and _same_content(tmp, previous[0]):
        os.remove(tmp)                      # nichts geändert seit der letzten Sicherung
        return None
    os.replace(tmp, dest)
    for old in list_backups(backup_dir)[max(1, keep):]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def backup(db: Database, backup_dir: str, keep: int = BACKUP_KEEP, label: str = "") -> str:
    """Konsistente Kopie der laufenden Datenbank anlegen; ältere über ``keep`` hinaus löschen. → Pfad"""
    if label and not re.fullmatch(r"[a-z0-9-]+", label):
        raise ValueError("Ungültige Bezeichnung für eine Sicherung: %r" % label)
    return _backup_con(db.con, backup_dir, keep, label)


def restore_backup(backup_path: str, db_path: str) -> None:
    """Sicherung zurückspielen. Die Datenbank darf dabei nirgends geöffnet sein."""
    src = sqlite3.connect("file:%s?mode=ro" % backup_path.replace("?", "%3f").replace("#", "%23"), uri=True)
    try:
        ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise sqlite3.DatabaseError("Sicherung ist beschädigt: %s" % ok)
        version = int(src.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DatabaseTooNew("Die Sicherung stammt von einer neueren b2stacker-Version (Schema %d)." % version)
        dst = _connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
