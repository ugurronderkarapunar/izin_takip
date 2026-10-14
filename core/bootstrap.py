"""Uygulama açılışında bir kez çalışan hazırlıklar."""
from __future__ import annotations

from . import storage


def init() -> None:
    storage.ensure_dirs()
    storage.setup_logging()
    
    # Döngüsel içe aktarma hatalarını önlemek için servisler burada yüklenir
    from . import calendar_utils, services
    
    calendar_utils.ensure_holidays()
    if hasattr(services, "migrate"):
        services.migrate()
