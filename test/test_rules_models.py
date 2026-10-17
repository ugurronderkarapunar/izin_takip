from datetime import date

import pytest
from pydantic import ValidationError

from core import models, rules


FULL = {date(2026, 10, 29)}
HALF = {date(2026, 10, 28)}


def test_leave_days_variants():
    mon, tue, sat = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 24)
    assert rules.leave_days("tam", None, mon, tue, FULL, HALF) == 2
    assert rules.leave_days("tam", None, date(2026, 10, 26), date(2026, 10, 30), FULL, HALF) == 3.5  # arife 0.5, bayram 0
    assert rules.leave_days("yarim_sabah", None, mon, mon, FULL, HALF) == 0.5
    assert rules.leave_days("saatlik", 3, mon, mon, FULL, HALF) == 0.375
    assert rules.leave_days("saatlik", 6, date(2026, 10, 28), date(2026, 10, 28), FULL, HALF) == 0.5  # arife günü üst sınır
    assert rules.leave_days("tam", None, sat, sat, FULL, HALF) == 0
    with pytest.raises(ValueError, match="tek güne"):
        rules.leave_days("yarim_sabah", None, mon, tue, FULL, HALF)
    with pytest.raises(ValueError, match="saat"):
        rules.leave_days("saatlik", None, mon, mon, FULL, HALF)
    with pytest.raises(ValueError, match="aşamaz"):
        rules.leave_days("saatlik", 9, mon, mon, FULL, HALF)


def _r(tip, saat=None, d="2026-10-19", e=None):
    return {"baslangic": d, "bitis": e or d, "sure_tipi": tip, "saat": saat}


def test_conflicts():
    assert rules.conflicts(_r("tam"), _r("tam"))
    assert rules.conflicts(_r("tam"), _r("yarim_sabah"))
    assert not rules.conflicts(_r("yarim_sabah"), _r("yarim_ogle"))
    assert rules.conflicts(_r("yarim_sabah"), _r("yarim_sabah"))
    assert not rules.conflicts(_r("saatlik", 3), _r("saatlik", 4))      # 7/8 saat
    assert rules.conflicts(_r("saatlik", 5), _r("saatlik", 4))          # 9/8 saat
    assert not rules.conflicts(_r("tam", d="2026-10-19"), _r("tam", d="2026-10-20"))
    assert rules.conflicts(_r("tam", d="2026-10-19", e="2026-10-21"), _r("yarim_ogle", d="2026-10-20"))


def test_kidem_hakki_table():
    tablo = [{"min_yil": 1, "hak": 14}, {"min_yil": 6, "hak": 20}, {"min_yil": 15, "hak": 26}]
    giris = date(2020, 3, 10)
    assert rules.kidem_hakki(giris, 2020, tablo) == 0       # henüz 1 yıl dolmadı
    assert rules.kidem_hakki(giris, 2021, tablo) == 14
    assert rules.kidem_hakki(giris, 2025, tablo) == 14      # 5 yıl dahil
    assert rules.kidem_hakki(giris, 2026, tablo) == 20
    assert rules.kidem_hakki(date(2000, 1, 1), 2026, tablo) == 26


def test_models_strict_and_defaults():
    base = {"id": "IZN-1", "personel_adi": "A", "izin_turu": "Mazeret İzni", "baslangic": "2026-10-19", "bitis": "2026-10-19",
            "olusturan": "u", "olusturma_zamani": "2026-10-01 10:00:00", "son_guncelleyen": "u", "son_guncelleme": "2026-10-01 10:00:00"}
    ok = models.validate_dump(models.RequestModel, base)
    assert ok["durum"] == "Beklemede" and ok["sure_tipi"] == "tam" and ok["ekler"] == []
    with pytest.raises(ValidationError):                       # yazım hatası: bilinmeyen alan
        models.validate_dump(models.RequestModel, {**base, "sapp_girildi": True})
    with pytest.raises(ValidationError):                       # hatalı değer
        models.validate_dump(models.RequestModel, {**base, "durum": "Onayladi"})
    with pytest.raises(ValidationError):
        models.validate_dump(models.RequestModel, {**base, "baslangic": "19.10.2026"})
    with pytest.raises(ValidationError):
        models.validate_dump(models.RequestModel, {**base, "saat": 30})
    # normalize: bilinmeyen alanı atar, eksikleri doldurur
    n = models.normalize(models.RequestModel, {**base, "eski_alan": 1})
    assert "eski_alan" not in n and n["silindi"] is False
    assert isinstance(ValidationError.__mro__[1], type) and issubclass(ValidationError, ValueError)


def test_settings_validation():
    assert models.SettingsModel().esik_ayni_anda == 3
    with pytest.raises(ValidationError):
        models.SettingsModel(bildirim_saat="25:99")
    with pytest.raises(ValidationError):
        models.SettingsModel(gunluk_calisma_saati=0)
