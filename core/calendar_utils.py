"""Resmî tatiller (arife yarım gün dahil), iş günü hesabı ve günlük izin yoğunluğu."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from . import config, rules
from .models import HolidayModel, normalize, validate_dump
from .storage import JsonStore, meta_store, transaction

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
# Arife günleri (yarım gün)
_HALF = [(date(y, 10, 28), "Cumhuriyet Bayramı Arifesi") for y in (2026, 2027, 2028)] + [
    (date(2026, 3, 19), "Ramazan Bayramı Arifesi"), (date(2026, 5, 26), "Kurban Bayramı Arifesi"),
    (date(2027, 3, 8), "Ramazan Bayramı Arifesi"), (date(2027, 5, 15), "Kurban Bayramı Arifesi"),
]


def _seed() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for year in (2026, 2027, 2028):
        for m, d, name in _FIXED:
            out.append({"tarih": date(year, m, d).isoformat(), "ad": name, "yarim": False})
    for start, n, name in _RELIGIOUS:
        for i in range(n):
            out.append({"tarih": (start + timedelta(days=i)).isoformat(), "ad": f"{name} {i + 1}. Gün", "yarim": False})
    for d, name in _HALF:
        out.append({"tarih": d.isoformat(), "ad": f"{name} (yarım gün)", "yarim": True})
    return sorted(out, key=lambda h: h["tarih"])


def ensure_holidays() -> None:
    with transaction():
        meta = meta_store.read()
        if meta.get("tatiller_yuklendi"):
            return
        if not holidays_store.read():
            holidays_store.write(_seed())
        meta["tatiller_yuklendi"] = True
        meta_store.write(meta)


def add_missing_half_days() -> int:
    """Eski kurulumlara arife (yarım gün) kayıtlarını bir kez ekler; mevcut tarihlere dokunmaz."""
    with transaction():
        hol = [normalize(HolidayModel, h) for h in holidays_store.read()]
        have = {h["tarih"] for h in hol}
        new = [{"tarih": d.isoformat(), "ad": f"{name} (yarım gün)", "yarim": True} for d, name in _HALF if d.isoformat() not in have]
        if new:
            holidays_store.write(sorted(hol + new, key=lambda h: h["tarih"]))
        return len(new)


def load_holidays() -> list[dict[str, Any]]:
    return [normalize(HolidayModel, h) for h in holidays_store.read()]


def save_holidays(hol: list[dict[str, Any]]) -> None:
    holidays_store.write(sorted((validate_dump(HolidayModel, h) for h in hol), key=lambda h: h["tarih"]))


def holiday_info() -> tuple[set[date], set[date]]:
    """(tam gün tatiller, yarım gün tatiller / arifeler)"""
    full: set[date] = set()
    half: set[date] = set()
    for h in load_holidays():
        try:
            (half if h["yarim"] else full).add(date.fromisoformat(h["tarih"]))
        except ValueError:
            continue
    return full, half


def holiday_set() -> set[date]:
    return holiday_info()[0]


def is_workday(d: date, holidays: set[date]) -> bool:
    return d.weekday() not in config.WEEKEND_DAYS and d not in holidays


def work_days(start: date, end: date, holidays: set[date] | None = None, half: set[date] | None = None) -> float:
    """Aralıktaki iş günü sayısı (arife günleri 0.5 sayılır)."""
    if end < start:
        return 0.0
    if holidays is None or half is None:
        full, hf = holiday_info()
        holidays = full if holidays is None else holidays
        half = hf if half is None else half
    return sum(rules.day_value(start + timedelta(days=i), holidays, half) for i in range((end - start).days + 1))


def daily_entries(requests: Iterable[dict[str, Any]], start: date, end: date, statuses: set[str],
                  exclude: str | None = None) -> dict[date, list[dict[str, Any]]]:
    """Her gün için o güne denk gelen talepler (takvim ızgarası için)."""
    out: dict[date, list[dict[str, Any]]] = {start + timedelta(days=i): [] for i in range((end - start).days + 1)}
    ex = exclude.strip().casefold() if exclude else None
    for r in requests:
        if r["durum"] not in statuses or r["personel_adi"].strip().casefold() == ex:
            continue
        lo = max(date.fromisoformat(r["baslangic"]), start)
        hi = min(date.fromisoformat(r["bitis"]), end)
        for i in range((hi - lo).days + 1):
            out[lo + timedelta(days=i)].append(r)
    return out


def daily_people(requests: Iterable[dict[str, Any]], start: date, end: date, statuses: set[str],
                 exclude: str | None = None, min_fraction: float = 0.0) -> dict[date, set[str]]:
    """Her gün için izinli kişiler. min_fraction=0.5 verilirse saatlik kısa izinler sayılmaz."""
    out: dict[date, set[str]] = {}
    for d, items in daily_entries(requests, start, end, statuses, exclude).items():
        out[d] = {r["personel_adi"] for r in items
                  if rules.fraction(r.get("sure_tipi", "tam"), r.get("saat")) >= min_fraction}
    return out

# END-OF-FILE v3.0
