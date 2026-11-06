"""Anzeigeformate, die überall gleich sein sollen (Feedback 0.13.1: „Datumsformat in der ganzen App einheitlich“)."""
from __future__ import annotations

import datetime


def dm(night: str | None, year: bool | None = None) -> str:
    """Datum einer Nacht: TT.MM. – mit Jahr (TT.MM.JJJJ), wenn es nicht das laufende Jahr ist.
    ``year`` erzwingt (True) oder unterdrückt (False) das Jahr."""
    n = str(night or "")
    if len(n) < 10 or n[4] != "-" or n[7] != "-":
        return "?"
    if year is None:
        year = n[:4] != str(datetime.date.today().year)
    return "%s.%s.%s" % (n[8:10], n[5:7], n[:4] if year else "")


def dmy(night: str | None) -> str:
    """Datum immer mit Jahr: TT.MM.JJJJ."""
    return dm(night, year=True)
