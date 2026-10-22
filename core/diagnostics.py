"""Bağlantı hatalarını sadeleştirir: parolayı gizler, olası nedeni ve çözümü söyler."""
from __future__ import annotations

import re
from typing import Any

from . import config

_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("password authentication failed", "authentication failed", "no password supplied"),
     "Kullanıcı adı veya parola hatalı. Neon'da bağlantı adresini yeniden kopyalayın; parolada özel karakter (@ : / ? #) varsa URL-kodlayın."),
    (("could not translate host name", "name or service not known", "nodename nor servname", "temporary failure in name resolution"),
     "Sunucu adı çözülemedi: adresteki host adı yanlış ya da eksik kopyalanmış olabilir."),
    (("network is unreachable", "no route to host"),
     "Sunucuya ulaşılamıyor. Supabase kullanıyorsanız 'Direct connection' yerine 'Session pooler' adresini kullanın (Streamlit Cloud IPv6 desteklemez)."),
    (("connection refused",), "Bağlantı reddedildi: host/port yanlış ya da veritabanı kapalı."),
    (("timeout expired", "timed out", "connection timeout"),
     "Zaman aşımı. Neon ücretsiz planda boşta uyur; 10-20 saniye sonra 'Tekrar dene'ye basın. Sürerse adresi kontrol edin."),
    (("ssl", "tls"), "SSL hatası: adresin sonuna ?sslmode=require ekleyin."),
    (("no module named 'psycopg", "can't load plugin", "nosuchmodule"),
     "Veritabanı sürücüsü yüklü değil: requirements.txt içinde psycopg[binary] olmalı; uygulamayı yeniden başlatın."),
    (("could not parse sqlalchemy url", "invalid dsn", "missing \"=\"", "invalid connection option", "invalid integer value"),
     "Bağlantı adresi okunamadı. Secrets'ta database_url = \"postgresql://...\" biçiminde, tırnak içinde ve dosyanın EN ÜSTÜNDE "
     "(herhangi bir [bölüm] başlığından önce) olmalı. 'psql ...' komutunu değil yalnızca adresi yapıştırın."),
    (("database", "does not exist"), "Veritabanı adı yanlış: adresteki veritabanı adını kontrol edin."),
]
_PERMANENT = ("password authentication", "authentication failed", "does not exist", "could not translate", "invalid", "no module", "can't load plugin",
              "could not parse")


def sanitize(msg: str) -> str:
    msg = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", msg)
    return re.sub(r"(password=)\S+", r"\1***", msg, flags=re.I)


def is_permanent(exc: BaseException) -> bool:
    """Tekrar denemenin anlamsız olduğu (yanlış parola / adres) hatalar."""
    m = str(exc).lower()
    return any(k in m for k in _PERMANENT)


def describe_error(exc: BaseException) -> dict[str, Any]:
    msg = sanitize(" ".join(str(exc).split()))[:400]
    low = msg.lower()
    hint = next((h for keys, h in _HINTS if any(k in low for k in keys)),
                "Bağlantı adresini (secrets: database_url) ve veritabanının açık olduğunu kontrol edin.")
    return {"tur": type(exc).__name__, "mesaj": msg, "ipucu": hint}


def source_info() -> str:
    """Hangi veri deposunun denendiğini (parolasız) söyler."""
    url = config.database_url()
    if not url:
        return "database_url bulunamadı → dosya depolaması kullanılıyor."
    try:
        from sqlalchemy.engine import make_url
        u = make_url(url.replace("postgres://", "postgresql://", 1))
        return f"database_url algılandı: sürücü={u.get_backend_name()}, host={u.host}, veritabanı={u.database}, kullanıcı={u.username}"
    except Exception:
        return "database_url algılandı ama biçimi geçersiz (adres okunamadı)."

# END-OF-FILE v3.2
