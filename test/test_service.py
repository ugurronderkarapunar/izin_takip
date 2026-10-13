import json
from datetime import date, timedelta

import pytest

from core import audit, backup, calendar_utils, config, documents, services
from core.config import S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED

U = "Test Kullanıcı"
ADMIN, PLAN, MGR, PERS = "Admin", "Planlama", "Yönetici", "Personel"


def _monday(offset_weeks: int = 0) -> date:
    d = date.today() + timedelta(days=30 + 7 * offset_weeks)
    return d - timedelta(days=d.weekday())


def _mk(name: str, tur="Mazeret İzni", weeks=0, days=1, **kw) -> str:
    s = _monday(weeks)
    return services.create_request(U, PERS, name, tur, s, s + timedelta(days=days - 1), **kw)


def test_work_days_skips_weekend_and_holiday():
    # 2026-10-29 Perşembe Cumhuriyet Bayramı; 26-30 Ekim -> 4 iş günü
    assert calendar_utils.work_days(date(2026, 10, 26), date(2026, 10, 30)) == 4
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
    assert audit.verify_chain() == []
    assert audit.verify_consistency(services.load_requests()) == []
    original = config.AUDIT_LOG.read_text(encoding="utf-8")
    logs = json.loads(original)
    logs[0]["detay"] = "oynandı"
    config.AUDIT_LOG.write_text(json.dumps(logs, ensure_ascii=False), encoding="utf-8")
    try:
        assert audit.verify_chain()  # değişiklik yakalanmalı
    finally:
        config.AUDIT_LOG.write_text(original, encoding="utf-8")
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
    assert backup.backup_now("test").exists()
    assert backup.zip_bytes()[:2] == b"PK"
