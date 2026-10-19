"""Birim (çalışma düzeni): gemi personeli hafta sonu da çalışır; hafta sonu izin gününden sayılır."""
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from core import audit, calendar_utils, config, models, rules, services
from tests.test_ui import ROOT, page

U, R = "Test Kullanıcı", "Kullanıcı"
SHIP = (True, False)


def _weekday(off: int, wd: int) -> date:
    d = date.today() + timedelta(days=900 + 7 * off)
    d += timedelta(days=wd - d.weekday())
    hol = calendar_utils.holiday_set() | calendar_utils.holiday_info()[1]
    while d in hol:
        d += timedelta(days=7)
    return d


def test_rules_profiles():
    fri, mon = date(2026, 11, 6), date(2026, 11, 9)       # Cuma - Pazartesi (hafta sonu arada)
    assert rules.leave_days("tam", None, fri, mon, set(), set()) == 2          # ofis: hafta sonu sayılmaz
    assert rules.leave_days("tam", None, fri, mon, set(), set(), profil=SHIP) == 4
    sat = date(2026, 11, 7)
    assert rules.leave_days("tam", None, sat, sat, set(), set()) == 0
    assert rules.leave_days("tam", None, sat, sat, set(), set(), profil=SHIP) == 1
    assert rules.leave_days("yarim_sabah", None, sat, sat, set(), set(), profil=SHIP) == 0.5
    # resmî tatil: varsayılan gemi birimi tatili düşer, "resmî tatil çalışılır" işaretliyse saymaz değil sayar
    hol = {date(2026, 11, 10)}
    assert rules.leave_days("tam", None, date(2026, 11, 9), date(2026, 11, 11), hol, set(), profil=(True, False)) == 2
    assert rules.leave_days("tam", None, date(2026, 11, 9), date(2026, 11, 11), hol, set(), profil=(True, True)) == 3
    assert rules.leave_days("tam", None, date(2026, 11, 10), date(2026, 11, 10), set(), hol, profil=(True, True)) == 1   # arife tam gün
    assert rules.leave_days("tam", None, date(2026, 11, 10), date(2026, 11, 10), set(), hol, profil=(True, False)) == 0.5


def test_default_units_and_validation():
    s = services.get_settings()
    by = {b["ad"]: b for b in s["birimler"]}
    assert by["Ofis"]["hafta_sonu"] is False and by["Kaptan"]["hafta_sonu"] is True
    assert {"Baş Makinist", "Güverte", "Yağcı"} <= set(by)
    with pytest.raises(ValueError, match="birden fazla"):
        models.SettingsModel(birimler=[{"ad": "A"}, {"ad": "a"}])
    with pytest.raises(ValueError, match="En az bir"):
        models.SettingsModel(birimler=[])
    # eski kayıt (birim alanı yok) Ofis sayılır
    assert models.normalize(models.PersonModel, {"ad": "Eski Kayıt X"})["birim"] == "Ofis"


def test_weekend_leave_for_ship_crew_not_office():
    sat, sun = _weekday(0, 5), _weekday(0, 6)
    sun = sat + timedelta(days=1)
    with pytest.raises(ValueError, match="iş günü yok"):
        services.create_request(U, R, "Ofis Hafta Sonu", "Mazeret İzni", sat, sun)                       # varsayılan: Ofis
    tid = services.create_request(U, R, "Kaptan Hafta Sonu", "Mazeret İzni", sat, sun, birim="Kaptan")    # yeni personel + birim
    r = services.get_request(tid)
    assert r["is_gunu"] == 2 and services.get_person("Kaptan Hafta Sonu")["birim"] == "Kaptan"
    pv = services.preview_request("Yeni Gemici", "Mazeret İzni", sat, sun, birim="Yağcı")
    assert pv["errors"] == [] and pv["is_gunu"] == 2
    assert services.preview_request("Yeni Gemici", "Mazeret İzni", sat, sun)["errors"]                    # birim verilmezse Ofis
    with pytest.raises(ValueError, match="Tanımsız birim"):
        services.create_request(U, R, "Hatalı Birim", "Mazeret İzni", sat, sun, birim="Yok")
    assert audit.verify_chain() == [] and audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_balance_counts_weekends_for_crew():
    services.add_personnel(U, R, "Makinist Bakiye", hak=20, birim="Baş Makinist")
    services.add_personnel(U, R, "Ofis Bakiye", hak=20)
    fri = _weekday(1, 4)
    mon = fri + timedelta(days=3)
    a = services.create_request(U, R, "Makinist Bakiye", config.ANNUAL, fri, mon)
    b = services.create_request(U, R, "Ofis Bakiye", config.ANNUAL, fri, mon)
    services.update_status(U, R, a, "Onaylandı")
    services.update_status(U, R, b, "Onaylandı")
    assert services.get_request(a)["is_gunu"] == 4 and services.get_request(b)["is_gunu"] == 2
    assert services.balance_for("Makinist Bakiye", fri.year)["kullanilan"] == 4
    assert services.balance_for("Ofis Bakiye", fri.year)["kullanilan"] == 2


def test_changing_unit_recomputes_requests_and_balance():
    services.add_personnel(U, R, "Birim Değişen", hak=20)
    fri = _weekday(2, 4)
    tid = services.create_request(U, R, "Birim Değişen", config.ANNUAL, fri, fri + timedelta(days=3))
    services.update_status(U, R, tid, "Onaylandı")
    assert services.get_request(tid)["is_gunu"] == 2
    services.update_personnel(U, R, "Birim Değişen", birim="Güverte")
    assert services.get_request(tid)["is_gunu"] == 4
    assert services.balance_for("Birim Değişen", fri.year)["kullanilan"] == 4
    assert any(e["islem"] == "İş Günü Yeniden Hesaplandı" and "Birim Değişen" in e["detay"] for e in audit.logs_store.read())
    services.update_personnel(U, R, "Birim Değişen", birim="Ofis")
    assert services.get_request(tid)["is_gunu"] == 2
    with pytest.raises(ValueError, match="Tanımsız birim"):
        services.update_personnel(U, R, "Birim Değişen", birim="Yok")
    assert audit.verify_chain() == [] and audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_bulk_set_birim():
    for n in ("Toplu Birim A", "Toplu Birim B", "Toplu Birim C"):
        services.add_personnel(U, R, n)
    assert services.bulk_set_birim(U, R, ["Toplu Birim A", "Toplu Birim B"], "Yağcı") == 2
    assert services.bulk_set_birim(U, R, ["Toplu Birim A", "Toplu Birim C"], "Yağcı") == 1      # A zaten Yağcı
    assert {services.get_person(n)["birim"] for n in ("Toplu Birim A", "Toplu Birim B", "Toplu Birim C")} == {"Yağcı"}
    with pytest.raises(ValueError, match="seçilmedi"):
        services.bulk_set_birim(U, R, [], "Yağcı")


def test_settings_unit_safety():
    base = services.get_settings()["birimler"]
    services.add_personnel(U, R, "Lostromo Test", birim="Güverte")
    # kullanımdaki birim silinemez
    without_guverte = [b for b in base if b["ad"] != "Güverte"]
    with pytest.raises(ValueError, match="kullanılıyor"):
        services.save_settings(U, R, {"birimler": without_guverte})
    # yeni birim eklenebilir, resmî tatil çalışan birim tanımlanabilir; kullanılmıyorsa kaldırılabilir
    services.save_settings(U, R, {"birimler": base + [{"ad": "Lostromo", "hafta_sonu": True, "resmi_tatil": True}]})
    services.update_personnel(U, R, "Lostromo Test", birim="Lostromo")
    assert services.birim_profil("Lostromo") == (True, True)
    holiday = date(2027, 10, 29)                                 # Cumhuriyet Bayramı (Cuma), tatil verisinde var
    pv = services.preview_request("Lostromo Test", "Mazeret İzni", holiday, holiday)
    assert pv["errors"] == [] and pv["is_gunu"] == 1
    services.update_personnel(U, R, "Lostromo Test", birim="Güverte")
    services.save_settings(U, R, {"birimler": base})
    assert [b["ad"] for b in services.get_settings()["birimler"]] == [b["ad"] for b in base]


def test_import_with_birim_column():
    import pandas as pd
    sat = _weekday(3, 5)
    df = pd.DataFrame([{"Personel": "İçe Gemici", "İzin Türü": "Mazeret İzni", "Başlangıç": sat.strftime("%d.%m.%Y"),
                        "Bitiş": sat.strftime("%d.%m.%Y"), "Açıklama": "", "Birim": "Yağcı"},
                       {"Personel": "İçe Ofis", "İzin Türü": "Mazeret İzni", "Başlangıç": sat.strftime("%d.%m.%Y"),
                        "Bitiş": sat.strftime("%d.%m.%Y"), "Açıklama": "", "Birim": ""}])
    created, errors = services.import_requests(U, R, df)
    assert len(created) == 1 and len(errors) == 1 and "iş günü yok" in errors[0]
    assert services.get_person("İçe Gemici")["birim"] == "Yağcı"


# ----------------------------------------------------------------------------- arayüz
def test_ui_new_request_with_unit_on_weekend():
    at = page("new_request")
    n = at.session_state["yt_nonce"]
    k = lambda x: f"yt_{n}_{x}"  # noqa: E731
    at.selectbox(key=k("personel")).select("➕ Listede yok — yeni personel ekle").run()
    at.text_input(key=k("yeni_ad")).input("UI Gemi Personeli").run()
    sat = _weekday(5, 5)
    at.date_input(key=k("bas")).set_value(sat)
    at.date_input(key=k("bit")).set_value(sat)
    at.run()
    assert any("iş günü yok" in e.value for e in at.error)                              # birim Ofis iken hafta sonu olmaz
    at.selectbox(key=k("birim")).select("Yağcı").run()
    assert not at.error and any("hafta sonu çalışılır" in c.value for c in at.caption)
    next(b for b in at.button if b.label == "Talebi kaydet").click().run()
    r = next(x for x in services.load_requests() if x["personel_adi"] == "UI Gemi Personeli")
    assert r["is_gunu"] == 1 and services.get_person("UI Gemi Personeli")["birim"] == "Yağcı"


def test_ui_admin_changes_unit_and_bulk_assigns():
    services.add_personnel(U, R, "UI Birim Değiştir")
    services.add_personnel(U, R, "UI Toplu Birim")
    Y = date.today().year
    at = page("admin")
    at.selectbox(key="pe_person").select("UI Birim Değiştir").run()
    at.selectbox(key=f"pe_UI Birim Değiştir_{Y}_birim").select("Kaptan")
    next(b for b in at.button if "Kaydet" in b.label).click().run()
    assert services.get_person("UI Birim Değiştir")["birim"] == "Kaptan" and not at.exception
    at.multiselect(key="tb_names").set_value(["UI Toplu Birim"]).run()
    at.selectbox(key="tb_birim").select("Güverte").run()
    next(b for b in at.button if b.label == "Birimi ata").click().run()
    assert services.get_person("UI Toplu Birim")["birim"] == "Güverte"


def test_ui_settings_units_form_saves():
    at = page("admin")
    base = services.get_settings()["birimler"]
    i = len(base)                                        # ilk boş satır
    at.text_input(key=f"bi_ad{i}").set_value("Lostromo UI")
    at.checkbox(key=f"bi_hs{i}").check()
    next(b for b in at.button if "Ayarları kaydet" in b.label).click().run()
    by = {b["ad"]: b for b in services.get_settings()["birimler"]}
    assert by["Lostromo UI"]["hafta_sonu"] is True and not at.exception
    services.save_settings(U, R, {"birimler": base})      # temizle
    assert AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run().exception == []
