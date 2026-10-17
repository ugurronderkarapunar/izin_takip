"""Arayüz testleri (Streamlit AppTest ile başsız çalıştırma)."""
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import calendar_utils, services, verify

ROOT = Path(__file__).resolve().parent.parent
U, R = "Uğur", "Kullanıcı"
PAGES = ["dashboard", "new_request", "requests_list", "team_calendar", "balances", "reports", "audit_view", "admin", "notifications"]


@pytest.fixture(autouse=True)
def _env_restore():
    snap = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snap)
    services.save_settings(U, R, {"kullanicilar": []})


def _day(off: int) -> date:
    d = date.today() + timedelta(days=600 + 7 * off)
    d -= timedelta(days=d.weekday())
    hol = calendar_utils.holiday_set() | calendar_utils.holiday_info()[1]
    while d in hol:
        d += timedelta(days=7)
    return d


def page(name: str, user: str = U, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "views" / f"{name}.py"), default_timeout=60)
    at.session_state["kullanici"] = user
    at.session_state["rol"] = R
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def app(**state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def test_all_pages_render_without_errors():
    d = _day(0)
    services.create_request(U, R, "UI Sayfa Bir", "Yıllık İzin", d, d + timedelta(days=1))
    services.create_request(U, R, "UI Sayfa İki", "Mazeret İzni", d, d, sure_tipi="yarim_sabah")
    for p in PAGES:
        at = page(p)
        assert not at.exception, (p, [e.value for e in at.exception])


def test_app_renders_with_navigation():
    at = app()
    assert not at.exception and not at.error


def test_install_guard_names_the_broken_file(tmp_path):
    proj = tmp_path / "p"
    shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("__pycache__", "data", ".pytest_cache", "tests", ".git"))
    svc = proj / "core" / "services.py"
    txt = svc.read_text(encoding="utf-8")
    svc.write_text(txt[: txt.index("def requests_df")], encoding="utf-8")        # kesilmiş yükleme
    (proj / "views" / "balances.py").unlink()                                      # eksik dosya
    at = AppTest.from_file(str(proj / "app.py"), default_timeout=60).run()
    assert "Kurulum eksik" in at.error[0].value
    assert "core/services.py" in at.code[0].value and "views/balances.py" in at.code[0].value


def test_password_gate_and_public_verification(monkeypatch):
    services.save_settings(U, R, {"uygulama_url": "https://izin.example.app"})
    d = _day(1)
    tid = services.create_request(U, R, "UI Doğrula", "Mazeret İzni", d, d)
    k = verify.sign(services.get_request(tid))
    monkeypatch.setenv("IZIN_APP_PASSWORD", "s3cret")
    at = app()
    assert any(t.key == "_pw_input" for t in at.text_input)
    at.text_input(key="_pw_input").input("yanlis").run()
    assert any("hatalı" in e.value for e in at.error)
    at.text_input(key="_pw_input").input("s3cret").run()
    assert at.session_state["_auth_ok"] is True and not at.error
    # QR doğrulama sayfası parolasız, herkese açık
    pub = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    pub.query_params["dogrula"] = tid
    pub.query_params["k"] = k
    pub.run()
    assert not pub.text_input and ("geçerli" in pub.success[0].value if pub.success else "onaylanmamış" in pub.warning[0].value)
    assert "UI Doğrula" in pub.markdown[-1].value
    bad = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    bad.query_params["dogrula"] = tid
    bad.query_params["k"] = "0" * 20
    bad.run()
    assert "eşleşmiyor" in bad.error[0].value and "UI Doğrula" not in " ".join(m.value for m in bad.markdown)


def test_user_picker_and_sidebar_undo():
    services.save_settings(U, R, {"kullanicilar": ["Ali", "Veli"]})
    d = _day(2)
    tid = services.create_request("Ali", R, "UI Geri Al", "Mazeret İzni", d, d)
    at = app()
    picker = next(s for s in at.sidebar.selectbox if s.key == "kullanici")
    assert picker.options == ["Ali", "Veli"]
    at.sidebar.selectbox(key="kullanici").select("Ali").run()
    assert any("geri al" in e.label.lower() for e in at.sidebar.expander)
    btn = next(b for b in at.sidebar.button if b.label == "Geri al")
    btn.click().run()
    assert services.get_request(tid)["silindi"]            # oluşturma geri alındı -> arşive kaldırıldı
    assert not at.exception


def test_new_request_half_day_via_ui():
    at = page("new_request")
    n = at.session_state["yt_nonce"]
    k = lambda x: f"yt_{n}_{x}"  # noqa: E731
    at.selectbox(key=k("personel")).select("➕ Listede yok — yeni personel ekle").run()
    at.text_input(key=k("yeni_ad")).input("UI Yarım Gün").run()
    at.radio(key=k("sure")).set_value("yarim_ogle").run()
    d = _day(3)
    at.date_input(key=k("bas")).set_value(d).run()
    assert not at.error, [e.value for e in at.error]
    save = next(b for b in at.button if b.label == "Talebi kaydet")
    assert not save.disabled
    save.click().run()
    r = next(x for x in services.load_requests() if x["personel_adi"] == "UI Yarım Gün")
    assert r["sure_tipi"] == "yarim_ogle" and r["is_gunu"] == 0.5 and r["baslangic"] == r["bitis"] == d.isoformat()


def test_admin_personnel_form_saves_with_one_click():
    services.add_personnel(U, R, "UI Hak Test", hak=20)
    Y = date.today().year
    at = page("admin")
    at.selectbox(key="pe_person").select("UI Hak Test").run()
    at.number_input(key=f"pe_UI Hak Test_{Y}_hak").set_value(27.0)
    at.number_input(key=f"pe_UI Hak Test_{Y}_dev").set_value(4.0)
    at.text_input(key=f"pe_UI Hak Test_{Y}_ekip").set_value("Makine")
    next(b for b in at.button if "Kaydet" in b.label).click().run()
    b = services.balance_for("UI Hak Test", Y)
    assert (b["hak"], b["devreden"]) == (27.0, 4.0) and services.get_person("UI Hak Test")["ekip"] == "Makine"
    assert services.balance_for("UI Hak Test", Y + 1)["hak"] == 20
    at.text_input(key=f"pe_UI Hak Test_{Y}_ad").set_value("UI Hak Yeni")
    next(b_ for b_ in at.button if "Kaydet" in b_.label).click().run()
    assert services.get_person("UI Hak Yeni") and at.selectbox(key="pe_person").value == "UI Hak Yeni"


def test_bulk_panel_and_notification_center():
    ids = []
    for i, n in enumerate(["UI Toplu A", "UI Toplu B"]):
        d = _day(10 + i)
        ids.append(services.create_request("Mehmet", R, n, "Mazeret İzni", d, d))
    at = page("requests_list")
    at.session_state["liste_tablo_0"] = {"selection": {"rows": [0, 1], "columns": [], "cells": []}}
    at.run()
    assert any("Toplu işlem — 2 talep seçili" in e.label for e in at.expander)
    # bildirim merkezi: başka kullanıcının işlemi "yeni" görünür, okundu işaretleyince kaybolur
    assert page("notifications", "Zeynep UI").tabs[0].label.startswith("Yeni (")      # ilk giriş: geçmiş yığılmaz
    services.update_status("Mehmet", R, ids[0], "Onaylandı")
    at = page("notifications", "Zeynep UI")
    assert at.tabs[0].label == "Yeni (1)"
    next(b for b in at.button if "okundu" in b.label).click().run()
    assert page("notifications", "Zeynep UI").tabs[0].label == "Yeni (0)"


def test_calendar_month_navigation_and_grid():
    at = page("team_calendar")
    m0 = at.session_state["cal_m"]
    assert any("izn-cal" in md.value for md in at.markdown)
    next(b for b in at.button if "Sonraki" in b.label).click().run()
    assert at.session_state["cal_m"] == m0 % 12 + 1 and not at.exception


def test_requests_list_date_range_filter():
    d = _day(20)
    services.create_request(U, R, "UI Tarih Filtre", "Mazeret İzni", d, d)
    at = page("requests_list")
    total = len(at.dataframe[0].value)
    at.date_input(key="f_tarih").set_value((d, d)).run()
    names = list(at.dataframe[0].value["Personel"])
    assert "UI Tarih Filtre" in names and len(names) < total
