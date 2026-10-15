"""Yedekleme: günlük otomatik yedek, manuel yedek, ZIP dışa aktarma.

Not: Doğrulama anahtarı (.audit_key) güvenlik nedeniyle yedeğe/ZIP'e KONMAZ; ayrıca saklayın.
"""
from __future__ import annotations

import os
import shutil
import zipfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from . import config
from .storage import clear_cache, data_lock, log
from .utils import now

_DATA_FILES = [config.REQUEST_DB, config.AUDIT_LOG, config.PERSONNEL_DB, config.HOLIDAY_DB,
               config.SETTINGS_DB, config.META_DB, config.ANCHOR_DB]


def backup_now(label: str = "") -> Path:
    with data_lock():
        stamp = now().strftime("%Y-%m-%d_%H%M%S")
        dest = config.BACKUP_DIR / (stamp + (f"_{label}" if label else ""))
        dest.mkdir(parents=True, exist_ok=True)
        for f in _DATA_FILES:
            if f.exists():
                shutil.copy2(f, dest / f.name)
        log.info("Yedek alındı: %s", dest.name)
        return dest


def list_backups() -> list[str]:
    if not config.BACKUP_DIR.exists():
        return []
    return sorted((p.name for p in config.BACKUP_DIR.iterdir() if p.is_dir()), reverse=True)


def prune(keep_days: int) -> int:
    cutoff = (now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for name in list_backups():
        if name[:10] < cutoff:
            shutil.rmtree(config.BACKUP_DIR / name, ignore_errors=True)
            removed += 1
    return removed


def daily_backup(keep_days: int = 30) -> Path | None:
    """Bugün için yedek yoksa alır; eski yedekleri temizler."""
    today = now().strftime("%Y-%m-%d")
    if any(n.startswith(today) for n in list_backups()):
        return None
    dest = backup_now("otomatik")
    prune(keep_days)
    return dest


def zip_bytes() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in _DATA_FILES:
            if f.exists():
                z.write(f, f"data/{f.name}")
        if config.ATTACH_DIR.exists():
            for p in config.ATTACH_DIR.rglob("*"):
                if p.is_file():
                    z.write(p, f"data/attachments/{p.relative_to(config.ATTACH_DIR).as_posix()}")
    return buf.getvalue()


def restore(name: str) -> None:
    """Seçilen yedeğe döner. Öncesinde otomatik güvenlik yedeği alınır."""
    if name != Path(name).name or not (config.BACKUP_DIR / name).is_dir():
        raise ValueError("Yedek bulunamadı.")
    src = config.BACKUP_DIR / name
    with data_lock():
        backup_now("geri_yukleme_oncesi")
        for f in _DATA_FILES:
            b = src / f.name
            if b.exists():
                tmp = f.with_suffix(f.suffix + ".restore")
                shutil.copyfile(b, tmp)  # yeni mtime: önbellek bayat kalmaz
                os.replace(tmp, f)
        clear_cache()
        log.warning("Yedekten geri yüklendi: %s", name)

# END-OF-FILE v2.3
