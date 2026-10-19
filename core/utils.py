"""Genel yardımcılar: zaman, biçimlendirme, dosya adı temizleme."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from . import config


def now() -> datetime:
    return datetime.now(config.TZ)


def now_str() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def days_since(value: Any) -> int | None:
    d = parse_date(value)
    return None if d is None else (now().date() - d).days


def fmt_date(value: Any) -> str:
    d = parse_date(value)
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_dt(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return "—"


def safe_filename(name: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return base[:80] or "dosya"

# END-OF-FILE v3.1
