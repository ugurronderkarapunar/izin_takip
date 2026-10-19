"""Yerel dosya verisini (data/ klasörü) veritabanına taşır.

    python -m scripts.migrate_to_db --source data --database-url "postgresql://..."

Taşınanlar: tüm belgeler (talepler, personel, ayarlar, tatiller ...), denetim günlüğü (zincir bozulmadan),
ek dosyalar ve doğrulama anahtarı (.audit_key). Hedef veritabanı BOŞ olmalıdır (üzerine yazmayı reddeder).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def migrate(source: Path, database_url: str) -> dict[str, Any]:
    from core import audit, config, storage

    config.DATA_DIR = source
    config.AUDIT_JSONL = source / "audit_logs.jsonl"
    config.AUDIT_LOG = source / "audit_logs.json"
    config.ATTACH_DIR = source / "attachments"
    config.BACKUP_DIR = source / "backups"
    config.KEY_FILE = source / ".audit_key"
    config.LOCK_FILE = source / ".lock"
    src = storage.FileBackend()
    dst = storage.SqlBackend(database_url)
    if dst.audit_last() is not None or any(dst.read_doc(n, list if n in ("request_db", "personnel", "holidays", "anchors") else dict) for n in ("request_db", "personnel")):
        raise SystemExit("Hedef veritabanı boş değil; üzerine yazılmaz. Boş bir veritabanı kullanın.")
    lists = {"request_db", "personnel", "holidays", "anchors"}
    with dst.transaction():
        for n in storage.DOC_NAMES:
            if (source / f"{n}.json").exists():
                dst.write_doc(n, src.read_doc(n, list if n in lists else dict))
        entries = src.audit_read()
        dst.audit_replace_all(entries)
        files = 0
        for tid, stored, data in src.iter_files():
            dst.put_file(tid, stored, data)
            files += 1
        key_file = source / ".audit_key"
        if key_file.exists():  # eski HMAC kayıtlarının doğrulanabilmesi için anahtar da taşınır
            meta = dst.read_doc("meta", dict)
            meta["audit_key"] = key_file.read_text(encoding="utf-8").strip()
            dst.write_doc("meta", meta)
    storage.use_backend(dst)
    audit._KEY_CACHE = None
    problems = audit.verify_chain()
    return {"belge": len([n for n in storage.DOC_NAMES if (source / f"{n}.json").exists()]), "log": len(entries),
            "ek_dosya": files, "zincir_sorunlari": problems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data", help="yerel veri klasörü")
    ap.add_argument("--database-url", required=True)
    args = ap.parse_args()
    res = migrate(Path(args.source).resolve(), args.database_url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["zincir_sorunlari"]:
        print("UYARI: log zinciri doğrulanamadı; .audit_key dosyasının data/ içinde olduğundan emin olun.")
        return 1
    print("Tamam. Doğrulama anahtarını güvenle saklayın (IZIN_AUDIT_KEY olarak secrets'a eklemeniz önerilir).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# END-OF-FILE v3.1
