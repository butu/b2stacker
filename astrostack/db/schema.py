"""SQLite-Schema von b2stacker und seine Migrationen.

Die Datenbank ist die einzige Datenquelle (Konzept Abschnitt 2/4). Jede Migration ist ein Schritt von
Schema-Version n-1 auf n; die aktuelle Version steht in ``PRAGMA user_version``. Eine bereits ausgelieferte
Migration wird nie mehr geändert – Änderungen kommen immer als neue Migration ans Ende von ``MIGRATIONS``.

Begriffe:
- Dateien (``files``) sind global, über ihren kanonischen Pfad eindeutig. Dieselbe Datei (z. B. ein
  Flat-Ordner) kann zu mehreren Projekten gehören → Zuordnung über ``project_files``.
- Jeder automatische Ablaufschritt speichert seine Versionsnummer (``header_version``,
  ``frame_metrics.version``). Ändert sich ein Schritt, wird er nur für betroffene Dateien nachgerechnet.
- Zeiten sind ISO-8601-Text in UTC (``2026-11-04T19:30:00Z``).
"""
from __future__ import annotations

MIGRATIONS: list[str] = [
    # ---------------------------------------------------------------- 1: Grundschema (b2stacker 1.0)
    """
    CREATE TABLE projects (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
        folder        TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- Ordnername unter <workdir>/projects/
        description   TEXT NOT NULL DEFAULT '',
        object        TEXT,                                   -- Himmelsobjekt (aus Headern oder vom Nutzer)
        cover_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
        created_at    TEXT NOT NULL,
        opened_at     TEXT,
        stacked_at    TEXT
    );

    -- Felder (Mosaik-Panels). In der Oberfläche vorerst genau eines pro Projekt.
    CREATE TABLE fields (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (project_id, name)
    );

    -- Teleskop + Kamera: für die Kalibrier-Zuordnung und die Warnung bei fremden Frames.
    CREATE TABLE setups (
        id            INTEGER PRIMARY KEY,
        telescope     TEXT NOT NULL DEFAULT '',
        camera        TEXT NOT NULL DEFAULT '',
        focal_length  REAL,
        pixel_size    REAL,
        name          TEXT,
        UNIQUE (telescope, camera)
    );

    CREATE TABLE source_folders (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        added_at    TEXT NOT NULL,
        checked_at  TEXT,                                     -- letzter vollständiger Durchlauf von Schritt 1
        online      INTEGER NOT NULL DEFAULT 1,               -- 0 = Quelle offline (NAS aus, Platte getrennt)
        UNIQUE (project_id, path)
    );

    -- Schritt 1 (Entdecken) füllt path/size/mtime_ns, Schritt 2 (Header) die Kopfzeilen-Spalten,
    -- Schritt 3 (Vermessen) checksum und frame_metrics.
    CREATE TABLE files (
        id              INTEGER PRIMARY KEY,
        path            TEXT NOT NULL UNIQUE,                 -- kanonischer Pfad (normcase + realpath)
        size            INTEGER NOT NULL,
        mtime_ns        INTEGER NOT NULL,
        checksum        TEXT,                                 -- SHA-256 des Inhalts, berechnet in Schritt 3
        discovered_at   TEXT NOT NULL,
        missing_since   TEXT,                                 -- gesetzt, solange die Datei fehlt
        excluded_dir    INTEGER NOT NULL DEFAULT 0,           -- liegt in einem Ordner wie _rejected, _alt …
        header_version  INTEGER,                              -- NULL = Header noch nicht gelesen
        header_error    TEXT,
        type            TEXT,                                 -- light | flat | bias | dark | darkflat | unknown
        is_master       INTEGER NOT NULL DEFAULT 0,
        source          TEXT,                                 -- nina | asiair | ekos | …
        object          TEXT,
        filter          TEXT,
        filter_kind     TEXT,
        exposure        REAL,
        gain            REAL,
        "offset"        REAL,
        readout         TEXT,
        binning         INTEGER,
        width           INTEGER,
        height          INTEGER,
        bayer           TEXT,
        ccd_temp        REAL,
        date_obs        TEXT,
        night           TEXT,                                 -- Abenddatum der Beobachtungsnacht (lokal)
        setup_id        INTEGER REFERENCES setups(id) ON DELETE SET NULL,
        meta_json       TEXT                                  -- vollständiges FrameMeta
    );
    CREATE INDEX files_checksum ON files(checksum);
    CREATE INDEX files_size ON files(size, mtime_ns);

    -- Welche Datei gehört zu welchem Projekt (und Feld), über welchen Quellordner gefunden.
    CREATE TABLE project_files (
        project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        file_id           INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_folder_id  INTEGER REFERENCES source_folders(id) ON DELETE SET NULL,
        field_id          INTEGER REFERENCES fields(id) ON DELETE SET NULL,
        PRIMARY KEY (project_id, file_id)
    );
    CREATE INDEX project_files_file ON project_files(file_id);

    CREATE TABLE frame_metrics (
        file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        version      INTEGER NOT NULL,                        -- Analyseversion
        measured_at  TEXT NOT NULL,
        nstars       INTEGER,
        fwhm         REAL,
        ecc          REAL,
        bg           REAL,
        noise        REAL,
        snr          REAL,
        hotpixels    INTEGER,
        thumb        TEXT,                                    -- Dateiname im Vorschau-Cache (system/)
        data_json    TEXT                                     -- vollständige Messzeile
    );

    -- Nächte bzw. Aufnahmeblöcke eines Projekts.
    CREATE TABLE sessions (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        night       TEXT NOT NULL,
        filter      TEXT NOT NULL DEFAULT '',
        setup_id    INTEGER REFERENCES setups(id) ON DELETE SET NULL,
        started_at  TEXT,
        ended_at    TEXT,
        UNIQUE (project_id, night, filter, setup_id)
    );

    -- Verwenden / nicht verwenden / zum Löschen vorgemerkt – immer mit Grund in Klartext.
    CREATE TABLE file_status (
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        status      TEXT NOT NULL CHECK (status IN ('use', 'skip', 'delete')),
        origin      TEXT NOT NULL CHECK (origin IN ('auto', 'manual')),
        reason      TEXT NOT NULL DEFAULT '',
        decided_at  TEXT NOT NULL,
        PRIMARY KEY (project_id, file_id)
    );

    CREATE TABLE stacks (
        id                 INTEGER PRIMARY KEY,
        project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        field_id           INTEGER REFERENCES fields(id) ON DELETE SET NULL,
        name               TEXT NOT NULL,                     -- z. B. „2026-11-04 Ha 12h30“
        folder             TEXT NOT NULL,                     -- Ordner unter dem Projektordner
        status             TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
        created_at         TEXT NOT NULL,
        finished_at        TEXT,
        run_label          TEXT,                              -- Filter bzw. Lauf (RGB, Ha, OIII …)
        frames             INTEGER,
        exposure_s         REAL,
        nights             INTEGER,
        settings_json      TEXT,                              -- alle WBPP-Einstellungen samt Begründung
        pixinsight_version TEXT,
        wbpp_version       TEXT,
        reference_file_id  INTEGER REFERENCES files(id) ON DELETE SET NULL,
        quality_json       TEXT,                              -- FWHM, Rauschen, Sterne, Exzentrizität, Verlauf
        size_bytes         INTEGER,
        note               TEXT NOT NULL DEFAULT '',
        error              TEXT
    );
    CREATE INDEX stacks_project ON stacks(project_id, created_at);

    CREATE TABLE stack_files (
        stack_id  INTEGER NOT NULL REFERENCES stacks(id) ON DELETE CASCADE,
        file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        weight    REAL,
        PRIMARY KEY (stack_id, file_id)
    );

    -- Von WBPP erzeugte Master-Kalibrierdateien, wiederverwendbar solange der Schlüssel gleich ist.
    CREATE TABLE masters (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        type        TEXT NOT NULL CHECK (type IN ('bias', 'dark', 'flat', 'darkflat')),
        path        TEXT NOT NULL,
        key         TEXT NOT NULL,                            -- Hash aus Quell-Prüfsummen + relevanten Einstellungen
        key_json    TEXT NOT NULL,                            -- dieselben Bestandteile lesbar
        created_at  TEXT NOT NULL,
        UNIQUE (project_id, key)
    );

    CREATE TABLE stack_masters (
        stack_id   INTEGER NOT NULL REFERENCES stacks(id) ON DELETE CASCADE,
        master_id  INTEGER NOT NULL REFERENCES masters(id) ON DELETE CASCADE,
        PRIMARY KEY (stack_id, master_id)
    );

    -- Bearbeitete und fremde Bilder (JPEG, TIFF, PSD, XISF …); stack_id NULL = gehört zum ganzen Projekt.
    CREATE TABLE assets (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        stack_id       INTEGER REFERENCES stacks(id) ON DELETE SET NULL,
        path           TEXT NOT NULL,
        checksum       TEXT,
        size           INTEGER,
        added_at       TEXT NOT NULL,
        missing_since  TEXT,
        owned          INTEGER NOT NULL DEFAULT 0,            -- 1 = von b2stacker erzeugt (darf es löschen)
        UNIQUE (project_id, path)
    );

    -- Stacking-Plan je Projekt (Schritt 5): automatisch erzeugt, Änderungen des Nutzers getrennt in edits_json,
    -- damit eine Neuberechnung sie nie überschreibt.
    CREATE TABLE plans (
        project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        version     INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        plan_json   TEXT NOT NULL,
        edits_json  TEXT NOT NULL DEFAULT '{}'
    );

    -- Grundlage der History (allgemein und je Projekt).
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        at          TEXT NOT NULL,
        project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        data_json   TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_at ON events(at);
    CREATE INDEX events_project ON events(project_id, at);
    """,
    # ---------------------------------------------------------------- 2: Galerie (Neue Oberfläche, Teil 2)
    """
    -- 1 = aus der Galerie entfernt (die Datei bleibt; der Watcher nimmt sie nicht wieder auf)
    ALTER TABLE assets ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    -- Größe + Änderungszeit beim letzten Blick: Vorschaubild neu, wenn sich die Datei ändert
    ALTER TABLE assets ADD COLUMN mtime_ns INTEGER;
    CREATE INDEX assets_stack ON assets(stack_id);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)
