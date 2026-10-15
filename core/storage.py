"""JSON depolama katmanı: atomik yazım, dosya kilidi, okuma önbelleği, bozuk dosya yedeği."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator

from filelock import FileLock

from . import config

log = logging.getLogger("izin")
_WARNINGS: list[str] = []
_cache: dict[str, tuple[tuple[int, int], Any]] = {}
_cache_lock = threading.Lock()
_lock_init = threading.Lock()
_file_lock: FileLock | None = None


def ensure_dirs() -> None:
    for d in (config.DATA_DIR, config.ATTACH_DIR, config.BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    ensure_dirs()
    if log.handlers:
        return
    handler = RotatingFileHandler(config.LOG_FILE, maxBytes=500_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def pop_warnings() -> list[str]:
    out = list(_WARNINGS)
    _WARNINGS.clear()
    return out


def _get_lock() -> FileLock:
    global _file_lock
    with _lock_init:
        if _file_lock is None:
            ensure_dirs()
            _file_lock = FileLock(str(config.LOCK_FILE), timeout=30)
        return _file_lock


@contextmanager
def data_lock() -> Iterator[None]:
    """Süreçler arası dosya kilidi (aynı iş parçacığında iç içe kullanılabilir)."""
    with _get_lock():
        yield


class JsonStore:
    """Tek bir JSON dosyasını (liste ya da sözlük) yöneten küçük depo sınıfı."""

    def __init__(self, path: Path, default: Callable[[], Any] = list) -> None:
        self.path = path
        self.default = default

    def read(self) -> Any:
        ensure_dirs()
        key = str(self.path)
        if not self.path.exists():
            with data_lock():
                if not self.path.exists():
                    self._atomic_write(self.default())
        stat = self.path.stat()
        sig = (stat.st_mtime_ns, stat.st_size)
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] == sig:
                return copy.deepcopy(hit[1])
        data = self._load()
        stat = self.path.stat()
        with _cache_lock:
            _cache[key] = ((stat.st_mtime_ns, stat.st_size), copy.deepcopy(data))
        return data

    def write(self, data: Any) -> None:
        with data_lock():
            self._atomic_write(data)

    def _load(self) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, type(self.default())):
                raise ValueError("Beklenmeyen veri tipi")
            return data
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            stamp = datetime.now(config.TZ).strftime("%Y%m%d_%H%M%S")
            backup = self.path.with_name(f"{self.path.stem}.bozuk_{stamp}{self.path.suffix}")
            os.replace(self.path, backup)
            msg = f"{self.path.name} okunamadı; bozuk dosya '{backup.name}' olarak yedeklendi, boş dosya oluşturuldu."
            _WARNINGS.append(msg)
            log.error(msg)
            data = self.default()
            self._atomic_write(data)
            return data

    def _atomic_write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        stat = self.path.stat()
        with _cache_lock:
            _cache[str(self.path)] = ((stat.st_mtime_ns, stat.st_size), copy.deepcopy(data))


meta_store = JsonStore(config.META_DB, dict)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


@contextmanager
def transaction(*stores: JsonStore) -> Iterator[None]:
    """Kilit altında çalışır; blokta hata olursa verilen depoları eski haline döndürür.

    Böylece 'talep yazıldı ama log yazılamadı' gibi yarım kalmış işlemler oluşmaz.
    """
    with data_lock():
        snaps = [(s, s.read()) for s in stores]
        try:
            yield
        except BaseException:
            for store, snap in snaps:
                try:
                    if store.read() != snap:
                        store.write(snap)
                        log.error("İşlem geri alındı (rollback): %s", store.path.name)
                except Exception:  # geri alma da başarısız olursa asıl hatayı gizleme
                    log.exception("Rollback başarısız: %s", store.path.name)
            raise

# END-OF-FILE v2.3
