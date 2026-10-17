import json
from datetime import date, timedelta

import pytest

from core import audit, backup, calendar_utils, config, documents, services
from core.config import S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED

U = "Test Kullanıcı"
ADMIN, PLAN, MGR, PERS = "Admin", "Planlama", "Yönetici", "Personel"


def _monday(offset_weeks: int = 0) -> date:
    d = date.today() + timedelta(days=30 + 7 * offset_weeks)
    d -= timedelta(days=d.weekday())
    hol = calendar_utils.holiday_set()
    while d in hol:  # resmî tatile denk gelen pazartesiyi atla
        d += timedelta(days=7)
    return d


def _mk(name: str, tur="Mazeret İzni", weeks=0, days=1, **kw) -> str:
    s = _monday(weeks)
    return services.create_request(U, PERS, name, tur, s, s + timedelta(days=days - 1), **kw)


def test_work_days_skips_weekend_and_holiday():
    # 28 Ekim arife (yarım gün), 29 Ekim Cumhuriyet Bayramı: 26-30 Ekim -> 1 + 1 + 0.5 + 0 + 1
    assert calendar_utils.work_days(date(2026, 10, 26), date(2026, 10, 30)) == 3.5
    assert calendar_utils.work_days(date(2026, 10, 24), date(2026, 10, 25)) == 0


def test_create_and_log():
    tid = _mk("Ali Test")
    r = services.get_request(tid)
    assert r["durum"] == S_PENDING and r["is_gunu"] == 1
    assert services.history(tid)[0]["islem"] == "Talep Oluşturuldu"


def test_overlap_blocked():
    _mk("Veli Test", weeks=1)
    with pytest.raises(ValueError, match="çakışan"):
        _mk("veli test", weeks=1)


def test_weekend_only_rejected():
    sat = _monday(2) + timedelta(days=5)
    with pytest.raises(ValueError, match="iş günü"):
        services.create_request(U, PERS, "Hafta Sonu", "Mazeret İzni", sat, sat + timedelta(days=1))


def test_balance_blocks_then_override():
    services.add_personnel(U, PERS, "Bakiye Test", hak=2)
    s = _monday(3)
    with pytest.raises(services.BalanceError):
        services.create_request(U, PERS, "Bakiye Test", config.ANNUAL, s, s + timedelta(days=4))
    tid = services.create_request(U, PERS, "Bakiye Test", config.ANNUAL, s, s + timedelta(days=4), bakiye_override=True)
    assert any("bakiye aşımı" in e["detay"] for e in services.history(tid))


def test_permissions():
    tid = _mk("Yetki Test", weeks=4)
    with pytest.raises(PermissionError):
        services.update_status(U, PERS, tid, S_APPROVED)


def test_status_rules():
    tid = _mk("Durum Test", weeks=5)
    with pytest.raises(ValueError, match="Gerekçe|gerekçe"):
        services.update_status(U, MGR, tid, S_REJECTED, "")
    services.update_status(U, MGR, tid, S_REJECTED, "uygun değil")
    with pytest.raises(ValueError, match="geçiş"):
        services.update_status(U, MGR, tid, S_APPROVED)


def test_full_flow_and_delivery_correction():
    tid = _mk("Akış Test", weeks=6)
    services.update_status(U, MGR, tid, S_APPROVED)
    with pytest.raises(ValueError, match="çıktı"):
        services.mark_delivered(U, PLAN, tid, date.today(), "Ayşe")
    services.mark_printed(U, PLAN, tid)
    services.mark_delivered(U, PLAN, tid, date.today(), "Ayşe")
    with pytest.raises(ValueError, match="zaten"):
        services.mark_delivered(U, PLAN, tid, date.today(), "Ayşe")
    with pytest.raises(ValueError, match="gerekçe"):
        services.correct_delivery(U, PLAN, tid, date.today(), "Fatma", "")
    services.correct_delivery(U, PLAN, tid, date.today(), "Fatma", "yanlış isim girilmişti")
    r = services.get_request(tid)
    assert r["ik_teslim_alan"] == "Fatma" and r["teslim_gecmisi"][0]["eski_alan"] == "Ayşe"
    services.revoke_delivery(U, PLAN, tid, "yanlış kayıt")
    assert not services.get_request(tid)["ik_teslim"]
    services.mark_sap(U, PLAN, tid, date.today())
    services.revoke_sap(U, ADMIN, tid, "test")
    assert services.stage(services.get_request(tid)) == "Teslim bekliyor"


def test_attachment_roundtrip_and_validation():
    tid = _mk("Ek Test", weeks=7, ekler=[("rapor.pdf", b"%PDF-1.4 test")])
    r = services.get_request(tid)
    assert services.read_attachment(tid, r["ekler"][0]["dosya"]) == b"%PDF-1.4 test"
    with pytest.raises(ValueError, match="dosya türü"):
        services.add_attachment(U, PERS, tid, "virus.exe", b"x")


def test_chain_ok_then_tamper_detected():
    from core.storage import get_backend
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []
    original = audit.logs_store.read()
    tampered = [dict(e) for e in original]
    tampered[0]["detay"] = "oynandı"
    get_backend().audit_replace_all(tampered)
    try:
        assert audit.verify_chain()  # değişiklik yakalanmalı
    finally:
        get_backend().audit_replace_all(original)
    assert audit.verify_chain() == []


def test_truncation_detected_by_anchor_and_seal(tmp_path):
    # Bağımsız mini zincir üzerinde: sondan silme çapa ve mühürle yakalanır
    logs = []
    prev = audit.GENESIS
    for i in range(1, 4):
        e = {"log_id": i, "zaman": "x", "kullanici": "u", "rol": "r", "talep_id": "T", "islem": "i",
             "eski_durum": "-", "yeni_durum": "-", "detay": str(i), "algo": audit.ALGO, "onceki_hash": prev}
        e["hash"] = audit._digest(e, prev)
        prev = e["hash"]
        logs.append(e)
    anchors = [{"tarih": "x", "log_id": 3, "hash": logs[2]["hash"]}]
    assert audit.verify_chain(logs, anchors) == []
    assert audit.verify_chain(logs[:2], anchors)  # son kayıt silindi
    seal = audit.seal_text(logs)
    assert audit.check_seal(seal, logs)[0]
    assert not audit.check_seal(seal, logs[:2])[0]


def test_legacy_chain_still_verifies():
    prev = audit.GENESIS
    logs = []
    import hashlib
    for i in range(1, 3):
        e = {"log_id": i, "zaman": "x", "kullanici": "u", "talep_id": "T", "islem": "i",
             "eski_durum": "-", "yeni_durum": "-", "detay": ""}
        payload = json.dumps({k: e[k] for k in audit.LEGACY_FIELDS}, ensure_ascii=False, sort_keys=True)
        e["onceki_hash"] = prev
        e["hash"] = hashlib.sha256((prev + payload).encode()).hexdigest()
        prev = e["hash"]
        logs.append(e)
    assert audit.verify_chain(logs, []) == []


def test_alerts_and_queues():
    reqs = services.load_requests()
    q = services.queues(reqs)
    assert set(q) == {"onay", "cikti", "teslim", "sap"}
    s = services.get_settings()
    s["uyari_bekleyen_gun"] = 0
    assert any(a["kural"] == "onay" for a in services.alerts(reqs, s))


def test_import_and_export():
    import pandas as pd
    s = _monday(10)
    df = pd.DataFrame([
        {"Personel": "İçe Aktar Bir", "İzin Türü": "Mazeret İzni", "Başlangıç": s.strftime("%d.%m.%Y"), "Bitiş": s.strftime("%d.%m.%Y"), "Açıklama": ""},
        {"Personel": "İçe Aktar İki", "İzin Türü": "Yok Tür", "Başlangıç": s.strftime("%d.%m.%Y"), "Bitiş": s.strftime("%d.%m.%Y"), "Açıklama": ""},
    ])
    created, errors = services.import_requests(U, PLAN, df)
    assert len(created) == 1 and len(errors) == 1
    assert services.export_excel()[:2] == b"PK"


def test_documents_and_backup():
    r = services.load_requests()[0]
    assert documents.build_docx(r, {})[:2] == b"PK"
    if documents.pdf_available():
        assert documents.build_pdf(r, {})[:4] == b"%PDF"
    assert backup.backup_now("test") in backup.list_backups()
    assert backup.zip_bytes()[:2] == b"PK"


# --------------------------- silme / geri yükleme / rollback / yedek ---------------------------
def test_soft_delete_hides_everywhere_and_restore():
    services.add_personnel(U, PERS, "Silme Test", hak=10)
    s = _monday(20)
    tid = services.create_request(U, PERS, "Silme Test", config.ANNUAL, s, s + timedelta(days=1))
    services.update_status(U, MGR, tid, S_APPROVED)
    assert services.balance_for("Silme Test", s.year)["kullanilan"] == 2
    with pytest.raises(PermissionError):
        services.delete_request(U, MGR, tid, "x")
    with pytest.raises(ValueError, match="gerekçe"):
        services.delete_request(U, PLAN, tid, " ")
    services.delete_request(U, PLAN, tid, "yanlış girilmiş")
    assert tid not in [r["id"] for r in services.load_requests()]
    assert tid in [r["id"] for r in services.load_requests(include_deleted=True)]
    assert services.get_request(tid)["silindi"]
    assert services.balance_for("Silme Test", s.year)["kullanilan"] == 0
    assert not any(a["talep_id"] == tid for a in services.alerts())
    with pytest.raises(ValueError, match="silinmiş"):
        services.update_status(U, MGR, tid, S_CANCELLED, "x")
    # aynı tarihlere yeniden talep girilebilir
    tid2 = services.create_request(U, PERS, "Silme Test", config.ANNUAL, s, s + timedelta(days=1))
    # çakışma varken geri yüklenemez
    with pytest.raises(ValueError, match="çakışan"):
        services.restore_request(U, PLAN, tid)
    services.update_status(U, MGR, tid2, S_CANCELLED, "vazgeçildi")
    services.restore_request(U, PLAN, tid, "geri geldi")
    assert not services.get_request(tid)["silindi"]
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []
    ops = [e["islem"] for e in services.history(tid)]
    assert "Talep Silindi" in ops and "Talep Geri Yüklendi" in ops


def test_rollback_on_failure(monkeypatch):
    tid = _mk("Rollback Test", weeks=21)
    n_logs = len(audit.logs_store.read())

    def boom(*a, **k):
        raise RuntimeError("log yazılamadı")

    monkeypatch.setattr(audit, "append_log", boom)
    with pytest.raises(RuntimeError):
        services.update_status(U, MGR, tid, S_APPROVED)
    monkeypatch.undo()
    assert services.get_request(tid)["durum"] == S_PENDING  # talep yarım güncellenmedi
    assert len(audit.logs_store.read()) == n_logs
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_rollback_restores_id_counter(monkeypatch):
    before = services.meta_store.read().get("seq")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(audit, "append_log", boom)
    with pytest.raises(RuntimeError):
        _mk("Sayaç Test", weeks=22)
    monkeypatch.undo()
    assert services.meta_store.read().get("seq") == before


def test_backup_restore_roundtrip():
    tid_before = _mk("Yedek Test A", weeks=23)
    name = backup.backup_now("rt")
    tid_after = _mk("Yedek Test B", weeks=24)
    assert services.get_request(tid_after)
    services.restore_backup(U, ADMIN, name)
    assert services.get_request(tid_before) is not None
    assert services.get_request(tid_after) is None
    assert any(e["islem"] == "Yedekten Geri Yüklendi" for e in audit.logs_store.read())
    with pytest.raises(ValueError):
        backup.restore("../etc")
    with pytest.raises(PermissionError):
        services.restore_backup(U, PLAN, name)


# --------------------------- yıl bazlı hak, yeniden adlandırma, talep düzenleme ---------------------------
def test_year_entitlement_only_affects_that_year():
    services.add_personnel(U, PERS, "Hak Test", hak=20)
    with pytest.raises(PermissionError):
        services.set_year_entitlement(U, PLAN, "Hak Test", 2027, 25, 3)
    services.set_year_entitlement(U, ADMIN, "Hak Test", 2027, 25, 3, "kıdem arttı")
    b27, b26 = services.balance_for("Hak Test", 2027), services.balance_for("Hak Test", 2026)
    assert (b27["hak"], b27["devreden"]) == (25, 3)
    assert (b26["hak"], b26["devreden"]) == (20, 0)  # diğer yıl etkilenmedi
    with pytest.raises(ValueError, match="0 ile 366"):
        services.set_year_entitlement(U, ADMIN, "Hak Test", 2027, -1, 0)
    assert any(e["islem"] == "İzin Hakkı Güncellendi" and "20 → 25" in e["detay"] for e in audit.logs_store.read())
    n = len(audit.logs_store.read())
    services.set_year_entitlement(U, ADMIN, "Hak Test", 2027, 25, 3)  # değişiklik yok -> log yok
    assert len(audit.logs_store.read()) == n


def test_update_personnel_persists():
    services.add_personnel(U, PERS, "Profil Test", hak=20)
    services.update_personnel(U, ADMIN, "Profil Test", not_="güncelleme", ekip="Makine", telefon="0532", aktif=False)
    p = services.get_person("Profil Test")
    assert (p["ekip"], p["telefon"], p["aktif"]) == ("Makine", "0532", False)


def test_rename_personnel_updates_requests():
    tid = _mk("Yazim Hatasi", weeks=30)
    services.rename_personnel(U, ADMIN, "Yazim Hatasi", "Yazım Hatası")
    assert services.get_request(tid)["personel_adi"] == "Yazım Hatası"
    assert services.get_person("Yazim Hatasi") is None and services.get_person("Yazım Hatası")
    with pytest.raises(ValueError, match="zaten var"):
        services.rename_personnel(U, ADMIN, "Yazım Hatası", "hak test")
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_edit_pending_request():
    tid = _mk("Düzenle Bir", weeks=31, days=1)
    s = _monday(31)
    services.edit_request(U, MGR, tid, "Mazeret İzni", s, s + timedelta(days=2), "tarih uzatıldı")
    r = services.get_request(tid)
    assert r["is_gunu"] == 3 and r["aciklama"] == "tarih uzatıldı" and r["durum"] == S_PENDING
    with pytest.raises(ValueError, match="Değişiklik yapılmadı"):
        services.edit_request(U, MGR, tid, "Mazeret İzni", s, s + timedelta(days=2), "tarih uzatıldı")
    with pytest.raises(PermissionError):
        services.edit_request(U, PERS, tid, "Mazeret İzni", s, s, "x")


def test_edit_ignores_self_overlap_but_blocks_other_overlap():
    a = _mk("Düzenle İki", weeks=32, days=1)
    b = _mk("Düzenle İki", weeks=33, days=1)
    s = _monday(32)
    services.edit_request(U, MGR, a, "Mazeret İzni", s, s + timedelta(days=1), "")  # kendisiyle çakışma sorun değil
    with pytest.raises(ValueError, match="çakışan"):
        services.edit_request(U, MGR, b, "Mazeret İzni", s + timedelta(days=1), s + timedelta(days=2), "")


def test_edit_approved_resets_approval_and_blocks_after_print():
    tid = _mk("Düzenle Üç", weeks=34, days=1)
    services.update_status(U, MGR, tid, S_APPROVED)
    s = _monday(34)
    with pytest.raises(ValueError, match="gerekçe"):
        services.edit_request(U, MGR, tid, "Mazeret İzni", s, s + timedelta(days=1), "")
    # yalnız açıklama değişirse onay korunur
    services.edit_request(U, MGR, tid, "Mazeret İzni", s, s, "not eklendi", "açıklama düzeltme")
    assert services.get_request(tid)["durum"] == S_APPROVED
    # tarih değişirse yeniden onaya düşer
    services.edit_request(U, MGR, tid, "Mazeret İzni", s, s + timedelta(days=1), "not eklendi", "tarih hatalıydı")
    r = services.get_request(tid)
    assert r["durum"] == S_PENDING and r["onay_zamani"] is None
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []
    # çıktısı alınmış talep düzenlenemez
    services.update_status(U, MGR, tid, S_APPROVED)
    services.mark_printed(U, PLAN, tid)
    with pytest.raises(ValueError, match="düzenlenemez"):
        services.edit_request(U, MGR, tid, "Mazeret İzni", s, s, "x", "y")
    assert not services.can_edit_request(services.get_request(tid))


def test_migration_moves_legacy_devreden_to_current_year():
    from core.storage import meta_store
    services.personnel_store.write(services.personnel_store.read() + [
        {"ad": "Eski Kayıt", "ekip": "", "telefon": "", "yillik_hak": 20, "devreden": 6, "aktif": True}])
    meta = meta_store.read()
    meta["schema_version"] = 3
    meta_store.write(meta)
    services.migrate()
    p = services.get_person("Eski Kayıt")
    year = date.today().year
    assert p["devreden"] == 0 and p["devreden_yillik"][str(year)] == 6
    assert services.balance_for("Eski Kayıt", year)["devreden"] == 6
    assert services.balance_for("Eski Kayıt", year + 1)["devreden"] == 0
    assert meta_store.read()["schema_version"] == services.SCHEMA_VERSION


# --------------------------- toplu işlemler ---------------------------
def test_bulk_approve_partial_and_permissions():
    ids = [_mk(f"Toplu {i}", weeks=40 + i) for i in range(3)]
    services.update_status(U, MGR, ids[2], S_REJECTED, "x")  # biri uygun değil
    with pytest.raises(PermissionError):
        services.bulk_apply(U, PLAN, ids, "onayla")
    res = services.bulk_apply(U, MGR, ids + ids[:1], "onayla", "toplu onay")  # tekrar eden id tek sayılır
    assert res["ok"] == ids[:2] and list(res["failed"]) == [ids[2]]
    assert all(services.get_request(i)["durum"] == S_APPROVED for i in ids[:2])
    assert services.get_request(ids[2])["durum"] == S_REJECTED
    assert "[Toplu işlem: 3 talep]" in services.history(ids[0])[-1]["detay"]
    assert audit.logs_store.read()[-1]["islem"] == "Toplu İşlem"
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []


def test_bulk_requires_reason_and_inputs():
    ids = [_mk(f"Toplu R {i}", weeks=50 + i) for i in range(2)]
    with pytest.raises(ValueError, match="gerekçe"):
        services.bulk_apply(U, MGR, ids, "reddet", "")
    with pytest.raises(ValueError, match="seçilmedi"):
        services.bulk_apply(U, MGR, [], "onayla")
    res = services.bulk_apply(U, MGR, ids, "reddet", "kontenjan dolu")
    assert len(res["ok"]) == 2 and all(services.get_request(i)["durum"] == S_REJECTED for i in ids)


def test_bulk_print_deliver_sap_chain():
    ids = [_mk(f"Toplu Z {i}", weeks=60 + i) for i in range(3)]
    services.bulk_apply(U, MGR, ids, "onayla")
    assert services.bulk_apply(U, PLAN, ids, "teslim", tarih=date.today(), alan="Ayşe")["ok"] == []  # önce çıktı gerekir
    assert services.bulk_apply(U, PLAN, ids, "cikti")["ok"] == ids
    with pytest.raises(ValueError, match="zorunlu"):
        services.bulk_apply(U, PLAN, ids, "teslim", tarih=date.today(), alan="")
    assert services.bulk_apply(U, PLAN, ids, "teslim", tarih=date.today(), alan="Ayşe")["ok"] == ids
    assert services.bulk_apply(U, PLAN, ids, "sap", tarih=date.today())["ok"] == ids
    assert all(services.stage(services.get_request(i)) == "Tamamlandı" for i in ids)
    assert services.bulk_apply(U, PLAN, ids, "sap", tarih=date.today())["ok"] == []  # tekrar uygulanmaz
    assert audit.verify_consistency(services.load_requests(include_deleted=True)) == []
