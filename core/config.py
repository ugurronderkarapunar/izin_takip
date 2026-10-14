"""Uygulama sabitleri, rol/yetki tablosu ve varsayılan ayarlar."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
# Veriyi başka bir klasöre almak için: IZIN_DATA_DIR ortam değişkeni
DATA_DIR = Path(os.environ.get("IZIN_DATA_DIR", BASE_DIR / "data"))

REQUEST_DB = DATA_DIR / "request_db.json"
AUDIT_LOG = DATA_DIR / "audit_logs.json"
PERSONNEL_DB = DATA_DIR / "personnel.json"
HOLIDAY_DB = DATA_DIR / "holidays.json"
SETTINGS_DB = DATA_DIR / "settings.json"
META_DB = DATA_DIR / "meta.json"
ANCHOR_DB = DATA_DIR / "anchors.json"
KEY_FILE = DATA_DIR / ".audit_key"
LOCK_FILE = DATA_DIR / ".lock"
ATTACH_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
LOG_FILE = DATA_DIR / "app.log"

TZ = ZoneInfo("Europe/Istanbul")

LEAVE_TYPES = ["Yıllık İzin", "Mazeret İzni", "Hastalık / Rapor", "Ücretsiz İzin", "İdari İzin", "Diğer"]
ANNUAL = "Yıllık İzin"

S_PENDING = "Beklemede"
S_APPROVED = "Onaylandı"
S_REJECTED = "Reddedildi"
S_CANCELLED = "İptal Edildi"
ALL_STATUSES = [S_PENDING, S_APPROVED, S_REJECTED, S_CANCELLED]
STATUS_ICON = {S_PENDING: "🟡", S_APPROVED: "🟢", S_REJECTED: "🔴", S_CANCELLED: "⚫"}

# Reddedildi ve İptal Edildi nihai durumlardır
ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    S_PENDING: [S_APPROVED, S_REJECTED, S_CANCELLED],
    S_APPROVED: [S_CANCELLED],
    S_REJECTED: [],
    S_CANCELLED: [],
}
ACTIVE_STATUSES = {S_PENDING, S_APPROVED}

STAGES = ["Onay bekliyor", "Çıktı bekliyor", "Teslim bekliyor", "SAP bekliyor", "Tamamlandı", "Kapalı", "Silindi"]

# NOT: Roller kimlik doğrulama değildir; arayüzü sadeleştirir ve işlemleri loglar.
ROLES = ["Personel", "Yönetici", "Planlama", "Admin"]
PERMISSIONS: dict[str, set[str]] = {
    "Personel": {"talep_gir", "ek_ekle"},
    "Yönetici": {"talep_gir", "ek_ekle", "onay"},
    "Planlama": {"talep_gir", "ek_ekle", "cikti", "teslim", "sap", "duzeltme", "import", "sil"},
    "Admin": {"talep_gir", "ek_ekle", "onay", "cikti", "teslim", "sap", "duzeltme", "import", "sil", "admin"},
}


def can(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role, set())


WEEKEND_DAYS = {5, 6}  # Cumartesi, Pazar

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"}
MAX_UPLOAD_MB = 10

DEFAULT_SETTINGS: dict[str, Any] = {
    "yillik_hak_varsayilan": 20,
    "esik_ayni_anda": 3,          # aynı gün izinli olabilecek kişi sayısı eşiği
    "uyari_bekleyen_gun": 2,      # onay bekleyen talep uyarısı (gün)
    "uyari_cikti_gun": 1,         # onaylandı ama çıktı alınmadı (gün)
    "uyari_teslim_gun": 2,        # çıktı alındı ama İK'ya teslim edilmedi (gün)
    "uyari_sap_gun": 3,           # onaylandı ama SAP girişi yok (gün)
    "yedek_saklama_gun": 30,
    "yonetici_telefon": "", "yonetici_eposta": "",
    "ik_telefon": "", "ik_eposta": "",
    "kayitli_filtreler": {},
}
