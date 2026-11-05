"""Verlauf in Klartext (Konzept: History) – aus der Tabelle ``events``, neueste zuerst.

Aufeinanderfolgende gleichartige Ereignisse eines Projekts werden zusammengefasst, damit der Verlauf lesbar bleibt:
neue Dateien einer Nacht (der Watcher meldet sie während der Aufnahme in vielen kleinen Portionen), mehrere
Frame-Entscheidungen, mehrere Planänderungen, mehrere Läufe eines Stack-Klicks. Ein Eintrag hat Titel und Zusatz
in Klartext, ein Symbol und einen Ton (ok/warn/err/info); die Oberfläche fügt nur noch die relative Zeit hinzu.
Allgemeiner Verlauf und Projekt-Verlauf sind dieselbe Liste, einmal gefiltert.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

from . import db as dbm

GAP_H = {"files_discovered": 12, "frames_decided": 2, "plan_edited": 2, "stack_done": 0.5, "stack_failed": 0.5,
         "stack_cancelled": 0.5, "assets_added": 6, "folder_added": 1, "stack_deleted": 1}
TYPE_NAMES = {"light": ("Light", "Lights"), "flat": ("Flat", "Flats"), "bias": ("Bias", "Bias"),
              "dark": ("Dark", "Darks"), "darkflat": ("Darkflat", "Darkflats"), "unknown": ("andere Datei",
                                                                                            "andere Dateien")}


def hours(h) -> str:
    """Stunden wie in der Oberfläche: unter 10 h mit einer Nachkommastelle, sonst ganz („2,5 h“, „14 h“)."""
    h = float(h or 0)
    if 0 < h < 10:
        return ("%.1f h" % h).replace(".", ",")
    return "%d h" % round(h)


def _n(n: int, kind: str) -> str:
    one, many = TYPE_NAMES.get(kind, (kind, kind))
    return "%d %s" % (n, one if n == 1 else many)


def _day(s: str | None) -> str:
    try:
        d = _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s or "")
    return "%02d.%02d.%d" % (d.day, d.month, d.year)


def _ts(s: str) -> float:
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ Zusammenfassen

def _merge_discovered(datas: list[dict]) -> dict:
    groups: dict[tuple, dict] = {}
    new = 0
    for d in datas:
        new += int(d.get("new") or 0)
        for g in d.get("groups") or []:
            k = (g.get("type"), g.get("filter"), g.get("night"))
            t = groups.setdefault(k, dict(type=k[0], filter=k[1], night=k[2], n=0, hours=0.0))
            t["n"] += int(g.get("n") or 0)
            t["hours"] += float(g.get("hours") or 0)
    items = list(groups.values())
    return dict(new=new, groups=items, nights=sorted({g["night"] for g in items if g["night"]}),
                light_hours=round(sum(g["hours"] for g in items if g["type"] == "light"), 2))


def _discovered_text(d: dict) -> tuple[str, str, str | None]:
    groups = d.get("groups") or []
    if not groups:                                 # alter Eintrag ohne Einzelheiten
        return "%d neue Dateien gefunden" % int(d.get("new") or 0), "", None
    lights = [g for g in groups if g["type"] == "light"]
    others: dict[str, int] = {}
    for g in groups:
        if g["type"] != "light":
            others[g["type"]] = others.get(g["type"], 0) + g["n"]
    parts = []
    if lights:
        n = sum(g["n"] for g in lights)
        by_f: dict[str, float] = {}
        for g in lights:
            by_f[g["filter"] or "?"] = by_f.get(g["filter"] or "?", 0) + g["hours"]
        filt = sorted(by_f.items(), key=lambda x: -x[1])
        if len(filt) == 1:
            title = "%s · %s · %s" % (_n(n, "light").replace("Light", "neue Light", 1) if n != 1 else "1 neues Light",
                                      filt[0][0], hours(filt[0][1]))
        else:
            title = "%s · %s" % (_n(n, "light").replace("Light", "neue Light", 1),
                                 ", ".join("%s %s" % (f, hours(h)) for f, h in filt[:4]) +
                                 (" …" if len(filt) > 4 else ""))
        if others:
            parts.append("dazu " + ", ".join(_n(v, k) for k, v in sorted(others.items(), key=lambda x: -x[1])))
    else:
        title = "Neue Kalibrierdaten: " + ", ".join(_n(v, k) for k, v in sorted(others.items(), key=lambda x: -x[1]))
    nights = sorted({g["night"] for g in (lights or groups) if g.get("night")})
    if len(nights) == 1:
        parts.insert(0, "Nacht vom " + _day(nights[0]))
    elif nights:
        parts.insert(0, "%d Nächte, %s – %s" % (len(nights), _day(nights[0]), _day(nights[-1])))
    return title, " · ".join(parts), (nights[-1] if nights else None)


def _stack_text(evs: list[dict]) -> tuple[str, str]:
    runs = []
    for e in evs:
        d = e["data"]
        runs.append("%s %s" % (d.get("run") or "Stack", hours(d.get("hours"))))
    lights = sum(int(e["data"].get("lights") or 0) for e in evs)
    return "Gestackt: " + ", ".join(reversed(runs)), "%d Lights" % lights if lights else ""


DECISIONS = {"use": "verwendet", "skip": "aussortiert", "auto": "auf Automatik gesetzt", "delete": "zum Löschen vorgemerkt"}


def describe(kind: str, evs: list[dict]) -> dict:
    """Ein (zusammengefasster) Eintrag → Titel, Zusatz, Symbol, Ton."""
    d = evs[0]["data"]
    out = dict(icon="info", tone="info", detail="", night=None)
    if kind == "files_discovered":
        merged = _merge_discovered([e["data"] for e in evs])
        out["title"], out["detail"], out["night"] = _discovered_text(merged)
        out.update(icon="image", tone="ok", data=merged)
    elif kind == "stack_done":
        out["title"], out["detail"] = _stack_text(evs)
        out.update(icon="layers", tone="ok")
    elif kind == "stack_failed":
        out.update(title="Stacken fehlgeschlagen: %s" % ", ".join(e["data"].get("run") or "?" for e in evs),
                   detail=d.get("error") or "", icon="warn", tone="err")
    elif kind == "stack_cancelled":
        out.update(title="Stacken abgebrochen: %s" % ", ".join(e["data"].get("run") or "?" for e in evs),
                   icon="unavail", tone="warn")
    elif kind == "stack_deleted":
        out.update(title="Stack gelöscht" if len(evs) == 1 else "%d Stacks gelöscht" % len(evs), icon="close",
                   detail=", ".join(e["data"].get("name") or "" for e in evs if e["data"].get("name")))
    elif kind == "frames_decided":
        by: dict[str, int] = {}
        for e in evs:
            by[e["data"].get("decision")] = by.get(e["data"].get("decision"), 0) + int(e["data"].get("n") or 0)
        out.update(title="Frames von Hand: " + ", ".join("%d %s" % (n, DECISIONS.get(k, k)) for k, n in by.items()),
                   icon="hand")
    elif kind == "plan_edited":
        out.update(title="Plan angepasst" + (" (%d Änderungen)" % len(evs) if len(evs) > 1 else ""), icon="sliders")
    elif kind == "project_created":
        out.update(title="Projekt angelegt", icon="plus", tone="ok")
    elif kind == "project_renamed":
        out.update(title="Umbenannt: %s → %s" % (d.get("old"), d.get("new")), icon="edit")
    elif kind == "folder_added":
        paths = [e["data"].get("path") or "" for e in evs]
        out.update(title="Ordner hinzugefügt" if len(paths) == 1 else "%d Ordner hinzugefügt" % len(paths),
                   detail=" · ".join(reversed(paths))[:400], icon="folder-new")
    elif kind == "folder_removed":
        out.update(title="Ordner entfernt", detail=d.get("path") or "", icon="folder")
    elif kind == "assets_added":
        n = sum(int(e["data"].get("n") or 0) for e in evs)
        watcher = all(e["data"].get("origin") == "watcher" for e in evs)
        out.update(title="%d %s in der Galerie" % (n, "Bild" if n == 1 else "Bilder"),
                   detail="im Projektordner gefunden" if watcher else
                   ", ".join(x for e in evs for x in e["data"].get("names") or [])[:300], icon="image", tone="ok")
    elif kind == "cover_set":
        out.update(title="Projektbild geändert" if d.get("asset") else "Projektbild zurückgesetzt", icon="star")
    elif kind == "backup_restored":
        out.update(title="Datenbank wiederhergestellt", icon="undo", tone="warn",
                   detail="Sicherung %s" % os.path.basename(d.get("backup") or ""))
    elif kind == "frames_deleted":
        mb = d.get("bytes", 0) / 1e9
        where = []
        if d.get("trash"):
            where.append("%d in den Papierkorb" % d["trash"])
        if d.get("permanent"):
            where.append("%d endgültig" % d["permanent"])
        out.update(title="%d %s gelöscht (%s GB)" % (d.get("n", 0), "Frame" if d.get("n") == 1 else "Frames",
                                                    ("%.1f" % mb).replace(".", ",")),
                   detail=" · ".join(where + [", ".join(d.get("names") or [])[:300]]), icon="close", tone="warn")
    elif kind == "stack_note":
        out.update(title="Notiz zu %s" % (d.get("name") or "Stack"), detail=d.get("text") or "", icon="edit")
    else:
        out.update(title=kind.replace("_", " "))
    return out


def timeline(db: dbm.Database, project_id: int | None = None, limit: int = 100) -> list[dict]:
    """Verlauf neueste zuerst, zusammengefasst. ``project_id`` None = alle Projekte (allgemeiner Verlauf)."""
    limit = max(1, min(int(limit), 1000))
    sql = ("SELECT e.id, e.at, e.kind, e.data_json, e.project_id, p.name AS project FROM events e "
           "LEFT JOIN projects p ON p.id = e.project_id")
    args: list = []
    if project_id is not None:
        sql += " WHERE e.project_id = ?"
        args.append(project_id)
    rows = db.query(sql + " ORDER BY e.at DESC, e.id DESC LIMIT ?", args + [min(limit * 6, 6000)])
    evs = []
    for r in rows:
        try:
            data = json.loads(r["data_json"] or "{}")
        except ValueError:
            data = {}
        evs.append(dict(id=r["id"], at=r["at"], kind=r["kind"], project_id=r["project_id"], project=r["project"],
                        data=data if isinstance(data, dict) else {}))
    # zusammenfassen: gleiche Art, gleiches Projekt, zeitlich nah (im Verlauf direkt nacheinander je Projekt)
    items: list[dict] = []
    open_group: dict[tuple, dict] = {}
    for e in evs:
        key = (e["project_id"], e["kind"])
        g = open_group.get(key)
        gap = GAP_H.get(e["kind"])
        if g is not None and gap is not None and _ts(g["events"][-1]["at"]) - _ts(e["at"]) <= gap * 3600:
            g["events"].append(e)
            continue
        g = dict(events=[e])
        open_group[key] = g
        items.append(g)
        # ein anderes Ereignis desselben Projekts schließt offene Gruppen dieses Projekts (Reihenfolge bleibt lesbar)
        for k in [k for k in open_group if k[0] == e["project_id"] and k != key]:
            del open_group[k]
    out = []
    for g in items[:limit]:
        es = g["events"]
        first = es[0]
        d = describe(first["kind"], es)
        out.append(dict(id=first["id"], at=first["at"], since=es[-1]["at"], kind=first["kind"],
                        project_id=first["project_id"], project=first["project"], count=len(es),
                        title=d["title"], detail=d.get("detail") or "", icon=d["icon"], tone=d["tone"],
                        night=d.get("night"), data=d.get("data") if "data" in d else first["data"]))
    return out
