"""Doğrulanabilir izin formu: izin kağıdındaki QR kod, imzalı bir bağlantıya götürür.

İmza, talebin değişmez alanlarından (no, personel, tür, tarih, süre) HMAC ile üretilir. Doğrulama sayfası
imzayı güncel kayıtla karşılaştırır: kâğıt sahteyse ya da talep yazdırıldıktan sonra düzenlenmişse eşleşmez;
eşleşirse talebin GÜNCEL durumu (ör. sonradan iptal edildi mi) gösterilir.
"""
from __future__ import annotations

import hashlib
import hmac
from io import BytesIO
from typing import Any
from urllib.parse import quote

from . import audit, config, services

_FIELDS = ("id", "personel_adi", "izin_turu", "baslangic", "bitis", "sure_tipi", "saat")


def canonical(r: dict[str, Any]) -> str:
    return "|".join(str(r.get(k)) for k in _FIELDS)


def sign(r: dict[str, Any]) -> str:
    key = hmac.new(audit._key(), b"izin-dogrulama-v1", hashlib.sha256).digest()
    return hmac.new(key, canonical(r).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def base_url() -> str:
    return (services.get_settings().get("uygulama_url") or config.env("app_url")).strip().rstrip("/")


def url_for(r: dict[str, Any]) -> str | None:
    base = base_url()
    return f"{base}/?dogrula={quote(r['id'])}&k={sign(r)}" if base else None


def qr_png(text: str, scale: int = 4) -> bytes:
    import segno
    buf = BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()


def check(talep_id: str, k: str) -> dict[str, Any]:
    """Döner: {"sonuc": gecerli | eslesmiyor | bulunamadi, "talep": {...} (yalnızca geçerliyse)}"""
    r = services.get_request(talep_id)
    if r is None or r["silindi"]:
        return {"sonuc": "bulunamadi"}
    if not hmac.compare_digest(sign(r), k or ""):
        return {"sonuc": "eslesmiyor"}  # ayrıntı verilmez: sahte kod ile kayıt bilgisi sızdırılmaz
    return {"sonuc": "gecerli", "talep": {
        "id": r["id"], "personel": r["personel_adi"], "tur": r["izin_turu"], "baslangic": r["baslangic"], "bitis": r["bitis"],
        "durum": r["durum"], "karar_veren": r["karar_veren"], "onay_zamani": r["onay_zamani"],
        "cikti_alindi": r["cikti_alindi"], "ik_teslim_tarihi": r["ik_teslim_tarihi"]}}

# END-OF-FILE v3.2
