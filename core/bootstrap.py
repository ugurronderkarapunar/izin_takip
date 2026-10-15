"""Uygulama açılışında bir kez çalışan hazırlıklar."""
from __future__ import annotations

from . import calendar_utils, services, storage


def init() -> None:
    storage.ensure_dirs()
    storage.setup_logging()
    calendar_utils.ensure_holidays()
    services.migrate()

# END-OF-FILE v2.3
