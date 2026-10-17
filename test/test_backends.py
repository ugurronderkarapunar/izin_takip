"""Arka uca özgü davranışlar (FileBackend / SqlBackend doğrudan örneklenir; küresel arka uç etkilenmez)."""
import json

import pytest

from core import audit, config, storage


@pytest.fixture()
def file_backend(tmp_path, monkeypatch):
    for name, rel in {"DATA_DIR": "", "AUDIT_JSONL": "audit_logs.jsonl", "AUDIT_LOG": "audit_logs.json", "ATTACH_DIR": "attachments",
                      "BACKUP_DIR": "backups", "KEY_FILE": ".audit_key", "LOCK_FILE": ".lock", "LOG_FILE": "app.log"}.items():
        monkeypatch.setattr(config, name, tmp_path / rel if rel else tmp_path)
    prev = storage._backend
    be = storage.FileBackend()
    storage.use_backend(be)
    audit._KEY_CACHE = None
    yield be
    storage.use_backend(prev)
    audit._KEY_CACHE = None


def test_file_audit_is_jsonl_append_only(file_backend):
    for i in range(3):
        audit.append_log("u", "Kullanıcı", f"T{i}", "Test", detay="x")
    lines = config.AUDIT_JSONL.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(json.loads(l)["log_id"] == i + 1 for i, l in enumerate(lines))
    assert audit.verify_chain() == []
    before = config.AUDIT_JSONL.read_bytes()
    audit.append_log("u", "Kullanıcı", "T3", "Test")
    assert config.AUDIT_JSONL.read_bytes().startswith(before)          # yalnızca sona ekleme


def test_file_audit_rolls_back_on_error(file_backend):
    audit.append_log("u", "Kullanıcı", "T0", "Test")
    size = config.AUDIT_JSONL.stat().st_size
    with pytest.raises(RuntimeError):
        with storage.transaction():
            audit.append_log("u", "Kullanıcı", "T1", "Test")
            storage.JsonStore("meta", dict).write({"x": 1})
            raise RuntimeError("yarıda kesildi")
    assert config.AUDIT_JSONL.stat().st_size == size
    assert storage.JsonStore("meta", dict).read() == {}
    assert audit.verify_chain() == []


def test_file_audit_repairs_partial_last_line(file_backend):
    audit.append_log("u", "Kullanıcı", "T0", "Test")
    with config.AUDIT_JSONL.open("ab") as f:
        f.write(b'{"log_id": 2, "zaman": "yarim')                      # çökme: yarım satır
    be = storage.FileBackend()
    storage.use_backend(be)
    assert len(be.audit_read()) == 1
    assert any("yarım yazılmış" in w for w in storage.pop_warnings())
    audit.append_log("u", "Kullanıcı", "T1", "Test")
    assert audit.verify_chain() == [] and len(be.audit_read()) == 2


def test_legacy_json_audit_is_migrated_to_jsonl(tmp_path, monkeypatch, file_backend):
    # eski sürüm: tek JSON dizisi (anahtarsız SHA-256 zinciri)
    import hashlib
    prev, entries = audit.GENESIS, []
    for i in (1, 2):
        e = {"log_id": i, "zaman": "x", "kullanici": "u", "talep_id": "T", "islem": "i", "eski_durum": "-", "yeni_durum": "-", "detay": ""}
        payload = json.dumps({k: e[k] for k in audit.LEGACY_FIELDS}, ensure_ascii=False, sort_keys=True)
        e["onceki_hash"] = prev
        e["hash"] = hashlib.sha256((prev + payload).encode()).hexdigest()
        prev = e["hash"]
        entries.append(e)
    config.AUDIT_LOG.write_text(json.dumps(entries), encoding="utf-8")
    be = storage.FileBackend()
    storage.use_backend(be)
    assert [e["log_id"] for e in be.audit_read()] == [1, 2]
    assert (tmp_path / "audit_logs.json.migrated").exists() and config.AUDIT_JSONL.exists()
    assert audit.verify_chain([*be.audit_read()], []) == []
    audit.append_log("u", "Kullanıcı", "T3", "Yeni")                   # yeni kayıt eski zincire bağlanır
    assert audit.verify_chain() == []


def test_corrupt_json_document_is_backed_up(file_backend):
    storage.JsonStore("meta", dict).write({"a": 1})
    (config.DATA_DIR / "meta.json").write_text("{bozuk", encoding="utf-8")
    file_backend.clear_caches()
    assert storage.JsonStore("meta", dict).read() == {}
    assert list(config.DATA_DIR.glob("meta.bozuk_*.json")) and storage.pop_warnings()


def test_sql_url_normalization_and_roundtrip(tmp_path):
    assert storage._normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert storage._normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    be = storage.SqlBackend(f"sqlite:///{tmp_path}/t.db")
    be.write_doc("meta", {"k": "v"})
    be.put_file("T1", "a.pdf", b"%PDF")
    assert be.read_doc("meta", dict) == {"k": "v"} and be.get_file("T1", "a.pdf") == b"%PDF"
    with pytest.raises(FileNotFoundError):
        be.get_file("T1", "yok.pdf")
    name = be.backup_create("t")
    be.write_doc("meta", {"k": "değişti"})
    be.backup_restore(name)
    assert be.read_doc("meta", dict) == {"k": "v"}
    with pytest.raises(RuntimeError):                                  # rollback: belge ve önbellek tutarlı
        with be.transaction():
            be.write_doc("meta", {"k": "yarım"})
            raise RuntimeError("x")
    assert be.read_doc("meta", dict) == {"k": "v"}
    be.event_add("hata", "t", "m")
    assert be.event_list(1)[0]["code"] == "t"


def test_migrate_local_files_to_database(tmp_path):
    from scripts import migrate_to_db
    src = tmp_path / "eski"
    src.mkdir()
    # eski sürüm veri klasörü: talepler + anahtarlı HMAC günlüğü + ek dosya
    prev_cfg = {k: getattr(config, k) for k in ("DATA_DIR", "AUDIT_JSONL", "AUDIT_LOG", "ATTACH_DIR", "BACKUP_DIR", "KEY_FILE", "LOCK_FILE")}
    prev_backend = storage._backend
    try:
        for k, rel in {"DATA_DIR": "", "AUDIT_JSONL": "audit_logs.jsonl", "AUDIT_LOG": "audit_logs.json", "ATTACH_DIR": "attachments",
                       "BACKUP_DIR": "backups", "KEY_FILE": ".audit_key", "LOCK_FILE": ".lock"}.items():
            setattr(config, k, src / rel if rel else src)
        audit._KEY_CACHE = None
        be = storage.FileBackend()
        storage.use_backend(be)
        storage.JsonStore("request_db", list).write([{"id": "IZN-1"}])
        audit.append_log("u", "Kullanıcı", "IZN-1", "Talep Oluşturuldu")
        audit.append_log("u", "Kullanıcı", "IZN-1", "Durum Değişti", "Beklemede", "Onaylandı")
        be.put_file("IZN-1", "ek.pdf", b"%PDF-ek")
        res = migrate_to_db.migrate(src, f"sqlite:///{tmp_path}/hedef.db")
        assert res["log"] == 2 and res["ek_dosya"] == 1 and res["zincir_sorunlari"] == []
        dst = storage.get_backend()
        assert dst.kind == "veritabanı" and dst.get_file("IZN-1", "ek.pdf") == b"%PDF-ek"
        with pytest.raises(SystemExit):                                # dolu hedefe ikinci kez taşıma reddedilir
            migrate_to_db.migrate(src, f"sqlite:///{tmp_path}/hedef.db")
    finally:
        for k, v in prev_cfg.items():
            setattr(config, k, v)
        storage.use_backend(prev_backend)
        audit._KEY_CACHE = None
