"""Uygulama sabitleri, rol/yetki tablosu ve varsayılan ayarlar."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_VERSION = "3.2"  # app.py, core/ ve ui/ aynı sürümde olmalı; karışırsa uygulama açılışta uyarır

BASE_DIR = Path(__file__).resolve().parent.parent
# Veriyi başka bir klasöre almak için: IZIN_DATA_DIR ortam değişkeni
DATA_DIR = Path(os.environ.get("IZIN_DATA_DIR", BASE_DIR / "data"))

REQUEST_DB = DATA_DIR / "request_db.json"
AUDIT_LOG = DATA_DIR / "audit_logs.json"          # eski sürüm (otomatik JSONL'e dönüştürülür)
AUDIT_JSONL = DATA_DIR / "audit_logs.jsonl"
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
ROLES = ["Kullanıcı"]  # tek rütbe: herkes her işlemi yapabilir (işlemler kullanıcı adıyla loglanır)
ALL_PERMS = {"talep_gir", "ek_ekle", "onay", "cikti", "teslim", "sap", "duzeltme", "import", "sil", "duzenle", "admin"}
PERMISSIONS: dict[str, set[str]] = {
    "Kullanıcı": set(ALL_PERMS),
    "Personel": {"talep_gir", "ek_ekle"},
    "Yönetici": {"talep_gir", "ek_ekle", "onay", "duzenle"},
    "Planlama": {"talep_gir", "ek_ekle", "cikti", "teslim", "sap", "duzeltme", "import", "sil", "duzenle"},
    "Admin": {"talep_gir", "ek_ekle", "onay", "cikti", "teslim", "sap", "duzeltme", "import", "sil", "duzenle", "admin"},
}

# Toplu işlemler: anahtar -> (etiket, gereken yetki)
BULK_ACTIONS: dict[str, tuple[str, str]] = {
    "onayla": ("✅ Onayla", "onay"),
    "reddet": ("❌ Reddet", "onay"),
    "iptal": ("🚫 İptal et", "onay"),
    "cikti": ("🖨️ Çıktı alındı", "cikti"),
    "teslim": ("📨 İK'ya teslim edildi", "teslim"),
    "sap": ("🧾 SAP'ye girildi", "sap"),
}


def can(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role, set())


WEEKEND_DAYS = {5, 6}  # Cumartesi, Pazar

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"}
MAX_UPLOAD_MB = 10

# Veritabanı bağlantısı: ortam değişkeni (Streamlit secrets'tan app.py aktarır). Boşsa dosya depolama kullanılır.
def database_url() -> str:
    return (os.environ.get("IZIN_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()


def env(name: str, default: str = "") -> str:
    """IZIN_<ADI> ortam değişkenini (secrets dahil) okur."""
    return os.environ.get(f"IZIN_{name.upper()}", default).strip()

# END-OF-FILE v3.2
