"""Stacking-Engines. Die gewählte Engine (Einstellung „engine“) bekommt den Plan – ohne stillen Fallback."""
from __future__ import annotations

ENGINES = ("pixinsight", "siril")
LABELS = {"pixinsight": "PixInsight", "siril": "Siril"}


def pixinsight_version(pi_path: str) -> str | None:
    """Versionszeile aus der PixInsight-Installation (neben der Programmdatei)."""
    import os
    pi_dir = os.path.dirname(os.path.dirname(pi_path or ""))
    for cand in (os.path.join(pi_dir, "doc", "PixInsight.txt"), os.path.join(pi_dir, "bin", "version.txt")):
        if os.path.exists(cand):
            try:
                return open(cand, encoding="utf-8", errors="replace").readline().strip()[:80] or None
            except OSError:
                pass
    return None


def engine_info(settings) -> dict:
    """Motor und Version für den Stack-Record. Fehler beim Ermitteln blockieren den Lauf nicht."""
    try:
        if settings.engine == "siril":
            from . import siril
            return dict(engine="siril", engine_version=siril.version_of(siril.locate(settings.siril)))
        return dict(engine="pixinsight", engine_version=pixinsight_version(settings.pixinsight))
    except Exception:  # noqa: BLE001
        return dict(engine=settings.engine, engine_version=None)


def _accepts(factory, n: int) -> bool:
    import inspect
    try:
        inspect.signature(factory).bind(*([None] * n))
    except (TypeError, ValueError):
        return False
    return True


def run_plan(plan: dict, settings, **kw):
    """Stacking-Lauf an die in den Einstellungen gewählte Engine weiterreichen."""
    # Imports erst beim Aufruf: die Engine-Module ziehen Bild- und Speicher-Module nach sich.
    from .pixinsight import StackError
    # Runner-Fabriken unterscheiden sich: PixInsight ohne Argumente, Siril (Skript, Ordner, Freigaben)
    arity = {"pixinsight": 0, "siril": 3}.get(settings.engine)
    factory = kw.get("runner_factory")
    if factory is not None and arity is not None and not _accepts(factory, arity):
        raise StackError("Die Runner-Fabrik passt nicht zur Engine %s (%d Argument(e) erwartet)."
                         % (settings.engine, arity))
    if settings.engine == "siril":
        from . import siril
        return siril.run_plan(plan, settings, **kw)
    if settings.engine == "pixinsight":
        from . import pixinsight
        return pixinsight.run_plan(plan, settings, **kw)
    raise StackError("Unbekannte Stacking-Engine: %r (erlaubt: %s)" % (settings.engine, ", ".join(ENGINES)))
