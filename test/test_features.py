import threading
from datetime import date, datetime, timedelta

import pytest

from core import audit, calendar_utils, config, documents, inbox, notifier, services, storage, verify
from core.config import S_APPROVED, S_CANCELLED, S_PENDING

U, R = "Test Kullanıcı", "Kullanıcı"


def _day(offset_weeks: int, weekday: int = 0) -> date:
    d = date.today() + timedelta(days=400 + 7 * offset_weeks)
    d += timedelta(days=weekday - d.weekday())
    hol = calendar_utils.holiday_set() | calendar_utils.holiday_info()[1]
    while d in hol:
        d += timedelta(days=7)
    return d


def _mk(name, tur="Mazeret İzni", w=0, wd=0, days=1, **kw):
    s = _day(w, wd)
    return services.create_request(U, R, name, tur, s, s + timedelta(days=days - 1), **kw)


# ----------------------------------------------------------------- yarım gün / saatlik / arife
def test_half_day_and_hourly_flow():
    s = _day(100)
    a = services.create_request(U, R, "Yarım Test", "Mazeret İzni", s, s + timedelta(days=3), sure_tipi="yarim_sabah")
    r = services.get_request(a)
    assert r["is_gunu"] == 0.5 and r["bitis"] == r["baslangic"]          # tek güne indirilir
    b = services.create_request(U, R, "Yarım Test", "Mazeret İzni", s, s, sure_tipi="yarim_ogle")  # diğer yarı: çakışmaz
    with pytest.raises(ValueError, match="çakışan"):
        services.create_request(U, R, "Yarım Test", "Mazeret İzni", s, s, sure_tipi="yarim_sabah")
    with pytest.raises(ValueError, match="çakışan"):
        services.create_request(U, R, "Yarım Test", "Mazeret İzni", s, s)
    t = _day(101)
    services.create_request(U, R, "Saat Test", "Mazeret İzni", t, t, sure_tipi="saatlik", saat=3)
    services.create_request(U, R, "Saat Test", "Mazeret İzni", t, t, sure_tipi="saatlik", saat=4)
    with pytest.raises(ValueError, match="çakışan"):
        services.create_request(U, R, "Saat Test", "Mazeret İzni", t, t, sure_tipi="saatlik", saat=2)
    with pytest.raises(ValueError, match="saat"):
        services.create_request(U, R, "Saat Test 2", "Mazeret İzni", t, t, sure_tipi="saatlik")
    assert services.get_request(b)["sure_tipi"] == "yarim_ogle"
    assert audit.verify_chain() == []


def test_half_day_deducts_fraction_from_balance():
    services.add_personnel(U, R, "Kesir Test", hak=10)
    s = _day(102)
    t1 = services.create_request(U, R, "Kesir Test", config.ANNUAL, s, s, sure_tipi="yarim_sabah")
    services.update_status(U, R, t1, S_APPROVED)
    t2 = services.create_request(U, R, "Kesir Test", config.ANNUAL, s + timedelta(days=1), s + timedelta(days=1), sure_tipi="saatlik", saat=2)
    b = services.balance_for("Kesir Test", s.year)
    assert b["kullanilan"] == 0.5 and b["bekleyen"] == 0.25 and b["kullanilabilir"] == 10 - 0.75
    assert services.get_request(t2)["is_gunu"] == 0.25


def test_arife_counts_half():
    a = date(2027, 10, 28)  # arife
    full, half = calendar_utils.holiday_info()
    assert a in half and a not in full
    assert calendar_utils.work_days(a, a) == 0.5


# ----------------------------------------------------------------- kıdemden otomatik hak
def test_auto_entitlement_precedence():
    services.add_personnel(U, R, "Kıdem Test", hak=20)
    p = services.get_person("Kıdem Test")
    assert services.entitlement(p, 2026) == (20.0, "varsayılan")
    services.update_personnel(U, R, "Kıdem Test", ise_giris="2015-06-01")
    assert services.balance_for("Kıdem Test", 2026)["hak"] == 20      # 11 yıl -> 20
    services.update_personnel(U, R, "Kıdem Test", ise_giris="2024-06-01")
    assert services.balance_for("Kıdem Test", 2026)["hak"] == 14
    assert services.balance_for("Kıdem Test", 2024)["hak"] == 0       # henüz 1 yıl dolmadı
    services.set_year_entitlement(U, R, "Kıdem Test", 2026, 30, 2)
    assert services.entitlement(services.get_person("Kıdem Test"), 2026)[1] == "elle girilmiş"
    services.clear_year_entitlement(U, R, "Kıdem Test", 2026)
    assert services.balance_for("Kıdem Test", 2026)["hak"] == 14
    with pytest.raises(ValueError, match="elle girilmiş"):
        services.clear_year_entitlement(U, R, "Kıdem Test", 2026)
    services.get_settings()
    services.save_settings(U, R, {"otomatik_hak": False})
    assert services.balance_for("Kıdem Test", 2026)["hak"] == 20      # varsayılana döner
    services.save_settings(U, R, {"otomatik_hak": True})
    with pytest.raises(ValueError):
        services.save_settings(U, R, {"gunluk_calisma_saati": 0})


# ----------------------------------------------------------------- tipli model koruması
def test_typo_in_service_code_is_rejected_and_rolled_back():
    tid = _mk("Model Test", w=103)
    before = services.get_request(tid)
    with pytest.raises(ValueError):
        with services._tx():
            reqs = [services._normalize(r) for r in services.requests_store.read()]
            r = next(x for x in reqs if x["id"] == tid)
            r.update(sapp_girildi=True)          # yazım hatası
            services._save_requests(reqs)
    assert services.get_request(tid) == before


# ----------------------------------------------------------------- geri alma (undo)
def _consistent():
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_undo_create_status_and_steps():
    tid = _mk("Undo Bir", w=104)
    assert services.recent_undoable()[0]["talep_id"] == tid
    services.update_status(U, R, tid, S_APPROVED)
    assert services.undo_last(U, R, tid) == "Durum Değişti"
    r = services.get_request(tid)
    assert r["durum"] == S_PENDING and r["onay_zamani"] is None
    _consistent()
    services.update_status(U, R, tid, S_APPROVED)
    services.mark_printed(U, R, tid)
    services.undo_last(U, R, tid)
    assert not services.get_request(tid)["cikti_alindi"]
    services.mark_printed(U, R, tid)
    services.mark_delivered(U, R, tid, date.today(), "Ayşe")
    services.undo_last(U, R, tid)
    assert not services.get_request(tid)["ik_teslim"]
    services.mark_delivered(U, R, tid, date.today(), "Ayşe")
    services.mark_sap(U, R, tid, date.today())
    services.undo_last(U, R, tid)
    assert not services.get_request(tid)["sap_girildi"] and services.get_request(tid)["ik_teslim"]
    _consistent()


def test_undo_blocked_after_newer_action_and_after_window(monkeypatch):
    tid = _mk("Undo İki", w=105)
    services.update_status(U, R, tid, S_APPROVED)
    services.mark_printed(U, R, tid)
    assert services.undo_last(U, R, tid) == "Çıktı Alındı"               # son işlem çıktıydı
    # son kayıt artık "geri alma" kaydı: tekrar geri alınamaz
    with pytest.raises(ValueError, match="geri alınabilir"):
        services.undo_last(U, R, tid)
    tid2 = _mk("Undo Üç", w=106)
    monkeypatch.setattr(services, "_age_minutes", lambda z: 999)
    with pytest.raises(ValueError, match="geri alınabilir"):
        services.undo_last(U, R, tid2)
    _consistent()


def test_undo_create_delete_edit():
    tid = _mk("Undo Dört", w=107)
    services.undo_last(U, R, tid)                                         # oluşturmayı geri al = arşive kaldır
    assert services.get_request(tid)["silindi"]
    services.restore_request(U, R, tid)
    s = _day(107)
    services.edit_request(U, R, tid, "Mazeret İzni", s, s + timedelta(days=1), "uzatıldı")
    assert services.get_request(tid)["is_gunu"] == 2
    services.undo_last(U, R, tid)
    r = services.get_request(tid)
    assert r["is_gunu"] == 1 and r["aciklama"] == "" and r["duzenleme_gecmisi"] == []
    services.delete_request(U, R, tid, "x")
    services.undo_last(U, R, tid)
    assert not services.get_request(tid)["silindi"]
    _consistent()


# ----------------------------------------------------------------- bildirim merkezi
def test_inbox_unread_and_mark_read():
    assert inbox.unread("Ayşe") == []                       # ilk giriş: geçmiş yığılmaz
    tid = _mk("Inbox Test", w=108)
    services.update_status("Mehmet", R, tid, S_APPROVED)
    un = inbox.unread("Ayşe")
    assert [e["islem"] for e in un][-1] == "Durum Değişti"
    assert "Mehmet" in inbox.describe(un[-1]) and "Onaylandı" in inbox.describe(un[-1])
    assert inbox.unread("Mehmet") == [] or all(e["kullanici"] != "Mehmet" for e in inbox.unread("Mehmet"))
    inbox.mark_read("Ayşe")
    assert inbox.unread("Ayşe") == []


# ----------------------------------------------------------------- doğrulanabilir form
def test_verification_token_flow():
    services.save_settings(U, R, {"uygulama_url": "https://izin.example.app"})
    tid = _mk("Doğrula Test", w=109)
    r = services.get_request(tid)
    url = verify.url_for(r)
    assert url.startswith("https://izin.example.app/?dogrula=") and verify.sign(r) in url
    k = verify.sign(r)
    res = verify.check(tid, k)
    assert res["sonuc"] == "gecerli" and res["talep"]["personel"] == "Doğrula Test" and res["talep"]["durum"] == S_PENDING
    assert verify.check(tid, "0" * 20)["sonuc"] == "eslesmiyor" and "talep" not in verify.check(tid, "0" * 20)
    assert verify.check("IZN-9999-0000", k)["sonuc"] == "bulunamadi"
    s = _day(109)
    services.edit_request(U, R, tid, "Mazeret İzni", s, s + timedelta(days=1), "")   # yazdırıldıktan sonra düzenlendi
    assert verify.check(tid, k)["sonuc"] == "eslesmiyor"
    assert verify.qr_png(url)[:4] == b"\x89PNG"
    doc = documents.build_docx(services.get_request(tid), {}, url)
    assert doc[:2] == b"PK"
    if documents.pdf_available():
        assert documents.build_pdf(services.get_request(tid), {}, url)[:4] == b"%PDF"
    services.update_status(U, R, tid, S_CANCELLED, "vazgeçildi")
    k2 = verify.sign(services.get_request(tid))
    assert verify.check(tid, k2)["talep"]["durum"] == S_CANCELLED       # iptal edilmiş belge güncel durumu gösterir
    services.delete_request(U, R, tid, "x")
    assert verify.check(tid, k2)["sonuc"] == "bulunamadi"


# ----------------------------------------------------------------- otomatik bildirim
def test_notifier_digest_dedup_and_failure(monkeypatch):
    monkeypatch.setenv("IZIN_SMTP_HOST", "smtp.test")
    services.save_settings(U, R, {"bildirim_aktif": True, "bildirim_saat": "00:00", "uyari_bekleyen_gun": 0,
                                  "ozet_alicilari": "ozet@x.com", "yonetici_eposta": "mudur@x.com"})
    storage_h = notifier.hist_store.read()
    storage_h["son_gonderim"] = None
    notifier.hist_store.write(storage_h)
    _mk("Bildirim Test", w=110)
    when = datetime.combine(_day(0), datetime.min.time()).replace(hour=10, tzinfo=config.TZ)
    sent = []
    monkeypatch.setattr(notifier, "send", lambda msgs, cfg: sent.extend(msgs))
    res = notifier.run_daily(at=when)
    assert res["durum"] == "gönderildi"
    to = {m["to"]: m for m in sent}
    assert "mudur@x.com" in to and "ozet@x.com" in to
    assert "Onay bekleyenler" in to["mudur@x.com"]["text"] and "Bildirim Test" in to["mudur@x.com"]["text"]
    assert notifier.run_daily(at=when)["durum"] == "bugün gönderildi"      # aynı gün ikinci kez gönderilmez
    # gönderim hatası: sahiplenme geri alınır, yeniden denenebilir
    h = notifier.hist_store.read(); h["son_gonderim"] = None; notifier.hist_store.write(h)

    def boom(msgs, cfg):
        raise OSError("smtp kapalı")

    monkeypatch.setattr(notifier, "send", boom)
    assert notifier.run_daily(at=when)["durum"] == "hata"
    assert notifier.hist_store.read()["son_gonderim"] is None
    assert any(e["code"] == "bildirim" for e in storage.recent_events(10))
    # kapalı / saat gelmedi / SMTP yok / hafta sonu
    early = when.replace(hour=0, minute=0)
    services.save_settings(U, R, {"bildirim_saat": "23:59"})
    assert notifier.run_daily(at=early)["durum"] == "saat gelmedi"
    sat = when + timedelta(days=(5 - when.weekday()) % 7 or 7)
    assert notifier.run_daily(at=sat)["durum"] == "iş günü değil"
    monkeypatch.delenv("IZIN_SMTP_HOST")
    assert notifier.run_daily(force=True)["durum"] == "smtp ayarı yok"
    services.save_settings(U, R, {"bildirim_aktif": False})
    assert notifier.run_daily()["durum"] == "kapalı"
    dry = notifier.run_daily(force=True, dry_run=True)
    assert dry["durum"] == "önizleme"


def test_notifier_integrity_problem_goes_to_summary_recipients():
    settings = services.get_settings() | {"ozet_alicilari": "ozet@x.com", "yonetici_eposta": ""}
    msgs = notifier.build_messages(settings, [], ["Log #3: içerik değiştirilmiş"], 2)
    assert len(msgs) == 1 and "bütünlük" in msgs[0]["subject"] and "2 hata" in msgs[0]["text"]
    assert notifier.build_messages(settings, [], [], 0) == []


# ----------------------------------------------------------------- kalıcılık ve eşzamanlılık
def test_data_survives_restart(backend_kind):
    tid = _mk("Kalıcılık Test", w=111)
    n_logs = len(audit.logs_store.read())
    prev = storage.get_backend()
    storage.use_backend(None)                       # "uygulamayı kapat-aç": arka uç ve önbellekler sıfırlanır
    try:
        assert storage.get_backend() is not prev
        assert services.get_request(tid)["personel_adi"] == "Kalıcılık Test"
        assert len(audit.logs_store.read()) == n_logs
        assert audit.verify_chain() == []
    finally:
        pass


def test_concurrent_writers_keep_chain_valid():
    names = [f"Eşzamanlı {i}" for i in range(12)]
    errors, ids = [], []

    def work(name, w):
        try:
            ids.append(_mk(name, w=200 + w))
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n, i)) for i, n in enumerate(names)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(ids)) == 12
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_events_and_ping():
    storage.log_event("hata", "deneme", "örnek hata")
    assert storage.recent_events(5)[0]["code"] == "deneme"
    assert storage.get_backend().ping()
    assert storage.get_backend().info()
