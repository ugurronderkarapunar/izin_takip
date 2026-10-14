"""Uygulama açılışında bir kez çalışan hazırlıklar."""
from __future__ import annotations

from . import storage


def init() -> None:
    storage.ensure_dirs()
    storage.setup_logging()
    
    # Döngüsel içe aktarmaya yol açan calendar_utils ve services çağrıları 
    # burada doğrudan yapılmaz; ihtiyaç duyulduğu ilgili view veya servis içinde çağrılır.
