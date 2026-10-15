"""Bildirim bağlantıları: WhatsApp / e-posta (kimlik bilgisi gerektirmez, kullanıcı tıklayıp gönderir)."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote


def whatsapp_link(phone: str | None, text: str) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    if digits.startswith("0"):
        digits = "90" + digits[1:]
    elif len(digits) == 10:
        digits = "90" + digits
    return f"https://wa.me/{digits}?text={quote(text)}"


def mailto_link(to: str | None, subject: str, body: str) -> str | None:
    if not to or not to.strip():
        return None
    return f"mailto:{to.strip()}?subject={quote(subject)}&body={quote(body)}"


def alert_message(alert: dict[str, Any]) -> tuple[str, str]:
    subject = f"İzin takibi: {alert['talep_id']}"
    body = (f"Merhaba, {alert['personel']} adlı personelin {alert['talep_id']} numaralı izin kaydı için "
            f"işlem bekleniyor: {alert['mesaj']}. Bilgilerinize.")
    return subject, body

# END-OF-FILE v2.3
