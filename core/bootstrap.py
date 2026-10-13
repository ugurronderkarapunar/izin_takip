"""Uygulama açılışında bir kez çalışan hazırlıklar."""
from __future__ import annotations

from . import calendar_utils, storage


def init() -> None:
    storage.ensure_dirs()
    storage.setup_logging()
    calendar_utils.ensure_holidays()
