"""İş kuralları (saf fonksiyonlar): yarım gün / saatlik izin, arife, çakışma, kıdeme göre izin hakkı."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from . import config

SURE_ETIKET = {"tam": "Tam gün", "yarim_sabah": "Yarım gün (sabah)", "yarim_ogle": "Yarım gün (öğleden sonra)", "saatlik": "Saatlik"}
GUNLUK_SAAT = 8.0


def sure_etiket(r: dict[str, Any]) -> str:
    if r.get("sure_tipi", "tam") == "saatlik":
        return f"Saatlik ({r.get('saat'):g} saat)" if r.get("saat") else "Saatlik"
    return SURE_ETIKET.get(r.get("sure_tipi", "tam"), "Tam gün")


def fmt_days(x: float | None) -> str:
    return "—" if x is None else f"{x:g}"


def slots(sure_tipi: str) -> frozenset[str]:
    return {"tam": frozenset("SÖ"), "yarim_sabah": frozenset("S"), "yarim_ogle": frozenset("Ö"), "saatlik": frozenset()}[sure_tipi]


def fraction(sure_tipi: str, saat: float | None, gunluk_saat: float = GUNLUK_SAAT) -> float:
    if sure_tipi == "tam":
        return 1.0
    if sure_tipi in ("yarim_sabah", "yarim_ogle"):
        return 0.5
    return min((saat or 0) / gunluk_saat, 1.0)


def conflicts(a: dict[str, Any], b: dict[str, Any], gunluk_saat: float = GUNLUK_SAAT) -> bool:
    """İki talep (baslangic, bitis, sure_tipi, saat) aynı kişide çakışıyor mu?"""
    a_s, a_e = date.fromisoformat(a["baslangic"]), date.fromisoformat(a["bitis"])
    b_s, b_e = date.fromisoformat(b["baslangic"]), date.fromisoformat(b["bitis"])
    if not (a_s <= b_e and b_s <= a_e):
        return False
    ta, tb = a.get("sure_tipi", "tam"), b.get("sure_tipi", "tam")
    sa, sb = slots(ta), slots(tb)
    if sa and sb:  # ikisi de tam/yarım gün: aynı dilimi paylaşıyorlarsa çakışır
        return bool(sa & sb)
    return fraction(ta, a.get("saat"), gunluk_saat) + fraction(tb, b.get("saat"), gunluk_saat) > 1.0 + 1e-9


def over_capacity(existing: list[dict[str, Any]], cand: dict[str, Any], gunluk_saat: float = GUNLUK_SAAT) -> dict[str, Any] | None:
    """Aynı kişinin o günkü TOPLAM izin payı (mevcut talepler + aday) bir günü aşıyorsa ilk ilgili talebi döndürür.

    İkili kontrol yetmez: 3 saat + 4 saat + 2 saat ikişer ikişer sığar ama toplam 9 saattir.
    """
    c_s, c_e = date.fromisoformat(cand["baslangic"]), date.fromisoformat(cand["bitis"])
    fc = fraction(cand.get("sure_tipi", "tam"), cand.get("saat"), gunluk_saat)
    for i in range((c_e - c_s).days + 1):
        d = c_s + timedelta(days=i)
        covering = [r for r in existing if date.fromisoformat(r["baslangic"]) <= d <= date.fromisoformat(r["bitis"])]
        total = sum(fraction(r.get("sure_tipi", "tam"), r.get("saat"), gunluk_saat) for r in covering)
        if covering and total + fc > 1.0 + 1e-9:
            return covering[0]
    return None


def day_value(d: date, full: set[date], half: set[date]) -> float:
    """O günün iş günü değeri: hafta sonu / resmî tatil 0, arife 0.5, diğer 1."""
    if d.weekday() in config.WEEKEND_DAYS or d in full:
        return 0.0
    return 0.5 if d in half else 1.0


def leave_days(sure_tipi: str, saat: float | None, start: date, end: date,
               full: set[date], half: set[date], gunluk_saat: float = GUNLUK_SAAT) -> float:
    """İzin süresi (iş günü olarak, kesirli olabilir)."""
    if end < start:
        return 0.0
    if sure_tipi == "tam":
        return sum(day_value(start + timedelta(days=i), full, half) for i in range((end - start).days + 1))
    if start != end:
        raise ValueError("Yarım gün ve saatlik izin tek güne girilir.")
    dv = day_value(start, full, half)
    if dv == 0:
        return 0.0
    if sure_tipi == "saatlik":
        if not saat or saat <= 0:
            raise ValueError("Saatlik izin için saat girilmelidir.")
        if saat > gunluk_saat:
            raise ValueError(f"Saatlik izin günlük çalışma süresini ({gunluk_saat:g} saat) aşamaz; tam gün seçin.")
        return min(round(saat / gunluk_saat, 4), dv)
    return 0.5 if (sure_tipi == "yarim_sabah" or dv == 1.0) else 0.0


def seniority_years(ise_giris: date, on: date) -> int:
    years = on.year - ise_giris.year - ((on.month, on.day) < (ise_giris.month, ise_giris.day))
    return max(years, 0)


def kidem_hakki(ise_giris: date, year: int, tablo: Iterable[dict[str, Any]]) -> float:
    """İşe giriş tarihine göre o yılın izin hakkı (yıl sonu itibarıyla tamamlanan hizmet yılı).

    Varsayılan tablo 4857 sayılı İş Kanunu m.53 asgari süreleridir; kurumunuzun toplu sözleşmesi
    farklıysa Yönetim > Ayarlar'dan değiştirin.
    """
    years = seniority_years(ise_giris, date(year, 12, 31))
    hak = 0.0
    for kademe in sorted(tablo, key=lambda k: k["min_yil"]):
        if years >= kademe["min_yil"]:
            hak = float(kademe["hak"])
    return hak

# END-OF-FILE v3.0
