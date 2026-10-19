"""Yedekleme: günlük otomatik yedek, manuel yedek, ZIP dışa aktarma, geri yükleme.

Dosya modunda yedekler data/backups klasöründe, veritabanı modunda veritabanındaki 'backups' tablosundadır
(sıkıştırılmış anlık görüntü; böylece bulutta kapat-aç yapınca yedekler de kaybolmaz).
Doğrulama anahtarı dosya modunda yedeğe/ZIP'e konmaz; veritabanı modunda meta belgesinin parçası olarak yedeklenir.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

from .storage import get_backend, log
from .utils import now


def backup_now(label: str = "") -> str:
    name = get_backend().backup_create(label)
    log.info("Yedek alındı: %s", name)
    return name


def list_backups() -> list[str]:
    return get_backend().backup_list()


def prune(keep_days: int) -> int:
    return get_backend().backup_prune(keep_days)


def daily_backup(keep_days: int = 30) -> str | None:
    """Bugün için yedek yoksa alır; eski yedekleri temizler."""
    today = now().strftime("%Y-%m-%d")
    if any(n.startswith(today) for n in list_backups()):
        return None
    name = backup_now("otomatik")
    prune(keep_days)
    return name


def restore(name: str) -> None:
    """Seçilen yedeğe döner (öncesinde otomatik güvenlik yedeği alınır)."""
    get_backend().backup_restore(name)
    log.warning("Yedekten geri yüklendi: %s", name)


def zip_bytes() -> bytes:
    b = get_backend()
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, raw in b.export_docs().items():
            z.writestr(f"data/{fname}", raw)
        for tid, stored, data in b.iter_files():
            z.writestr(f"data/attachments/{tid}/{stored}", data)
    return buf.getvalue()

# END-OF-FILE v3.1
