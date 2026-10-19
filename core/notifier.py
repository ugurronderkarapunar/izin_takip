"""Otomatik bildirim: günlük e-posta özeti (gecikmiş işler + bütünlük sorunları).

İki tetikleyici aynı kodu kullanır:
  1) Zamanlanmış çalıştırma:  python -m scripts.send_notifications   (GitHub Actions cron)
  2) Uygulama açılışında fırsatçı çalıştırma (gün içinde bir kez, ayarlanan saatten sonra)
Aynı gün ikinci kez gönderim, veritabanı işlemi içinde 'sahiplenme' kaydıyla engellenir.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any

from . import audit, calendar_utils, config, services, storage
from .storage import JsonStore, transaction
from .utils import now, now_str

hist_store = JsonStore("bildirim_gecmisi", dict)
_KIME = {"Yönetici": "yonetici_eposta", "İK": "ik_eposta", "Planlama": "planlama_eposta"}
_BASLIK = {"onay": "Onay bekleyenler", "cikti": "Çıktısı alınacaklar", "teslim": "İK'ya teslim edilecekler", "sap": "SAP'ye girilecekler"}


def smtp_config() -> dict[str, Any] | None:
    host = config.env("smtp_host")
    if not host:
        return None
    user = config.env("smtp_user")
    return {"host": host, "port": int(config.env("smtp_port", "587") or 587), "user": user,
            "password": config.env("smtp_password"), "sender": config.env("smtp_from") or user}


def split_addrs(text: str) -> list[str]:
    return [a for a in re.split(r"[,;\s]+", text or "") if "@" in a]


def build_messages(settings: dict[str, Any], alerts: list[dict[str, Any]], problems: list[str],
                   error_count: int = 0) -> list[dict[str, str]]:
    """Alıcı başına tek e-posta: sorumlu olduğu uyarılar; özet alıcıları her şeyi görür."""
    per: dict[str, list[dict[str, Any]]] = {}
    for a in alerts:
        for addr in split_addrs(settings.get(_KIME.get(a["kime"], ""), "")):
            per.setdefault(addr, []).append(a)
    summary = split_addrs(settings["ozet_alicilari"])
    for addr in summary:
        per[addr] = list(alerts)
    for addr in summary:
        per.setdefault(addr, [])
    out = []
    for addr, items in per.items():
        extra = addr in summary and (problems or error_count)
        if not items and not extra:
            continue
        subject, text, html = render(items, problems if addr in summary else [], error_count if addr in summary else 0,
                                     settings.get("uygulama_url", ""))
        out.append({"to": addr, "subject": subject, "text": text, "html": html})
    return out


def render(items: list[dict[str, Any]], problems: list[str], error_count: int, app_url: str) -> tuple[str, str, str]:
    crit = sum(a["seviye"] == "kritik" for a in items)
    subject = f"İzin takibi: {len(items)} bekleyen iş" + (f" ({crit} kritik)" if crit else "") + (" · ⚠️ bütünlük uyarısı" if problems else "")
    lines: list[str] = [f"İzin takibi günlük özet — {now().strftime('%d.%m.%Y')}", ""]
    html = [f"<h3>İzin takibi günlük özet — {now().strftime('%d.%m.%Y')}</h3>"]
    for kural in ("onay", "cikti", "teslim", "sap"):
        grp = [a for a in items if a["kural"] == kural]
        if not grp:
            continue
        lines.append(f"{_BASLIK[kural]} ({len(grp)})")
        html.append(f"<h4>{_BASLIK[kural]} ({len(grp)})</h4><ul>")
        for a in grp:
            mark = "🔴 " if a["seviye"] == "kritik" else ""
            lines.append(f"  - {mark}{a['talep_id']} · {a['personel']} — {a['mesaj']}")
            html.append(f"<li>{mark}<b>{escape(a['talep_id'])}</b> · {escape(a['personel'])} — {escape(a['mesaj'])}</li>")
        lines.append("")
        html.append("</ul>")
    if problems:
        lines += ["VERİ BÜTÜNLÜĞÜ UYARISI"] + [f"  - {p}" for p in problems[:10]] + [""]
        html.append("<h4 style='color:#c62828'>Veri bütünlüğü uyarısı</h4><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems[:10]) + "</ul>")
    if error_count:
        lines += [f"Son 24 saatte {error_count} hata kaydı var (Yönetim > Sistem durumu)."]
        html.append(f"<p>Son 24 saatte <b>{error_count}</b> hata kaydı var (Yönetim &gt; Sistem durumu).</p>")
    if app_url:
        lines.append(app_url)
        html.append(f"<p><a href='{escape(app_url)}'>Uygulamayı aç</a></p>")
    return subject, "\n".join(lines), "".join(html)


def send(messages: list[dict[str, str]], cfg: dict[str, Any]) -> None:
    if cfg["port"] == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
    try:
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        for m in messages:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = m["subject"], cfg["sender"], m["to"]
            msg.set_content(m["text"])
            msg.add_alternative(m["html"], subtype="html")
            server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


def _errors_24h() -> int:
    cutoff = (now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for e in storage.recent_events(200) if e["level"] == "hata" and e["ts"] >= cutoff)


def send_test(to: str) -> dict[str, str]:
    cfg = smtp_config()
    if cfg is None:
        return {"durum": "smtp ayarı yok"}
    try:
        send([{"to": to, "subject": "İzin takibi: test e-postası", "text": "Bu bir test e-postasıdır.", "html": "<p>Bu bir test e-postasıdır.</p>"}], cfg)
    except Exception as exc:  # ağ / kimlik doğrulama hataları
        storage.log_event("hata", "bildirim", exc)
        return {"durum": "hata", "hata": str(exc)}
    return {"durum": "gönderildi"}


def run_daily(force: bool = False, dry_run: bool = False, at: datetime | None = None) -> dict[str, Any]:
    """Günlük özeti gönderir. force=True: ayar/saat/iş günü/gün içi tekrar denetimlerini atlar."""
    settings = services.get_settings()
    at = at or now()
    if not settings["bildirim_aktif"] and not force:
        return {"durum": "kapalı"}
    if not force:
        if not calendar_utils.is_workday(at.date(), calendar_utils.holiday_set()):
            return {"durum": "iş günü değil"}
        if at.strftime("%H:%M") < settings["bildirim_saat"]:
            return {"durum": "saat gelmedi"}
    cfg = smtp_config()
    if cfg is None and not dry_run:
        return {"durum": "smtp ayarı yok"}
    alerts = services.alerts(settings=settings)
    rep = services.integrity_report()
    msgs = build_messages(settings, alerts, rep["chain"] + rep["consistency"], _errors_24h())
    if dry_run:
        return {"durum": "önizleme", "mesajlar": msgs, "uyari_sayisi": len(alerts)}
    today = at.strftime("%Y-%m-%d")
    with transaction():  # çift gönderimi önlemek için önce sahiplen
        h = hist_store.read()
        if not force and h.get("son_gonderim") == today:
            return {"durum": "bugün gönderildi"}
        h["son_gonderim"] = today
        hist_store.write(h)
    if not msgs:
        return {"durum": "gönderilecek bir şey yok", "uyari_sayisi": 0}
    try:
        send(msgs, cfg)  # type: ignore[arg-type]
    except Exception as exc:  # ağ / kimlik doğrulama hataları: yeniden denenebilsin
        with transaction():
            h = hist_store.read()
            h["son_gonderim"] = None
            hist_store.write(h)
        storage.log_event("hata", "bildirim", exc)
        return {"durum": "hata", "hata": str(exc)}
    with transaction():
        h = hist_store.read()
        h.update(son_basarili=now_str(), son_alici_sayisi=len(msgs))
        hist_store.write(h)
        audit.append_log("sistem", "Sistem", "-", "Bildirim Gönderildi", detay=f"{len(msgs)} e-posta, {len(alerts)} uyarı")
    return {"durum": "gönderildi", "gonderilen": len(msgs), "uyari_sayisi": len(alerts)}


def opportunistic() -> dict[str, Any] | None:
    """Uygulama açılışında çağrılır; hata verirse uygulamayı düşürmez."""
    try:
        return run_daily()
    except Exception as exc:
        storage.log_event("hata", "bildirim", exc)
        return None

# END-OF-FILE v3.1
