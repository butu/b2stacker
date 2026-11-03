"""Stacking-Engines. Die gewählte Engine (Einstellung „engine“) bekommt den Plan – ohne stillen Fallback."""
from __future__ import annotations

ENGINES = ("pixinsight", "siril")
LABELS = {"pixinsight": "PixInsight", "siril": "Siril"}


def run_plan(plan: dict, settings, **kw):
    """Stacking-Lauf an die in den Einstellungen gewählte Engine weiterreichen."""
    # Imports erst beim Aufruf: die Engine-Module ziehen Bild- und Speicher-Module nach sich.
    from .pixinsight import StackError
    if settings.engine == "siril":
        from . import siril
        return siril.run_plan(plan, settings, **kw)
    if settings.engine == "pixinsight":
        from . import pixinsight
        return pixinsight.run_plan(plan, settings, **kw)
    raise StackError("Unbekannte Stacking-Engine: %r (erlaubt: %s)" % (settings.engine, ", ".join(ENGINES)))
