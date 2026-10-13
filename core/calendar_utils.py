"""Resmî tatiller, iş günü hesabı ve günlük izin yoğunluğu."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from . import config
from .storage import JsonStore, data_lock, meta_store

holidays_store = JsonStore(config.HOLIDAY_DB, list)

_FIXED = [
    (1, 1, "Yılbaşı"),
    (4, 23, "Ulusal Egemenlik ve Çocuk Bayramı"),
    (5, 1, "Emek ve Dayanışma Günü"),
    (5, 19, "Atatürk'ü Anma, Gençlik ve Spor Bayramı"),
    (7, 15, "Demokrasi ve Milli Birlik Günü"),
    (8, 30, "Zafer Bayramı"),
    (10, 29, "Cumhuriyet Bayramı"),
]
# Dinî bayramlar her yıl değişir: Yönetim > Tatiller ekranından kontrol edin / güncelleyin.
_RELIGIOUS = [
    (date(2026, 3, 20), 3, "Ramazan Bayramı"),
    (date(2026, 5, 27), 4, "Kurban Bayramı"),
    (date(2027, 3, 9), 3, "Ramazan Bayramı"),
    (date(2027, 5, 16), 4, "Kurban Bayramı"),
]


def _seed() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for year in (2026, 2027, 2028):
        for m, d, name in _FIXED:
            out.append({"tarih": date(year, m, d).isoformat(), "ad": name})
    for start, n, name in _RELIGIOUS:
        for i in range(n):
            out.append({"tarih": (start + timedelta(days=i)).isoformat(), "ad": f"{name} {i + 1}. Gün"})
    return sorted(out, key=lambda h: h["tarih"])


def ensure_holidays() -> None:
    with data_lock():
        meta = meta_store.read()
        if meta.get("tatiller_yuklendi"):
            return
        if not holidays_store.read():
            holidays_store.write(_seed())
        meta["tatiller_yuklendi"] = True
        meta_store.write(meta)


def holiday_set() -> set[date]:
    out: set[date] = set()
    for h in holidays_store.read():
        try:
            out.add(date.fromisoformat(h["tarih"]))
        except (KeyError, ValueError):
            continue
    return out


def is_workday(d: date, holidays: set[date]) -> bool:
    return d.weekday() not in config.WEEKEND_DAYS and d not in holidays


def work_days(start: date, end: date, holidays: set[date] | None = None) -> int:
    if end < start:
        return 0
    hol = holiday_set() if holidays is None else holidays
    return sum(1 for i in range((end - start).days + 1) if is_workday(start + timedelta(days=i), hol))


def daily_people(requests: Iterable[dict[str, Any]], start: date, end: date,
                 statuses: set[str], exclude: str | None = None) -> dict[date, set[str]]:
    """Her gün için izinli kişilerin kümesi."""
    out: dict[date, set[str]] = {start + timedelta(days=i): set() for i in range((end - start).days + 1)}
    ex = exclude.strip().casefold() if exclude else None
    for r in requests:
        if r["durum"] not in statuses or r["personel_adi"].strip().casefold() == ex:
            continue
        lo = max(date.fromisoformat(r["baslangic"]), start)
        hi = min(date.fromisoformat(r["bitis"]), end)
        for i in range((hi - lo).days + 1):
            out[lo + timedelta(days=i)].add(r["personel_adi"])
    return out
