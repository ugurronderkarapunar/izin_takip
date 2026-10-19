"""Uygulama içi bildirim merkezi: başkalarının yaptığı işlemler (okundu bilgisi kullanıcı başına tutulur)."""
from __future__ import annotations

from typing import Any

from . import audit, services
from .storage import JsonStore, transaction

state_store = JsonStore("kullanici_durumu", dict)


def _max_id(logs: list[dict[str, Any]]) -> int:
    return logs[-1]["log_id"] if logs else 0


def unread(user: str) -> list[dict[str, Any]]:
    """Kullanıcının son baktığından sonra BAŞKALARININ yaptığı işlemler. İlk girişte geçmiş yığılmaz."""
    if not user.strip():
        return []
    logs = audit.logs_store.read()
    st = state_store.read()
    if user not in st:
        with transaction():
            st = state_store.read()
            st.setdefault(user, {"son_gorulen_log": _max_id(logs)})
            state_store.write(st)
        return []
    last = st[user]["son_gorulen_log"]
    out = []
    for e in reversed(logs):
        if e["log_id"] <= last:
            break
        if e["kullanici"] not in (user, "sistem"):
            out.append(e)
    return out[::-1]


def mark_read(user: str) -> None:
    with transaction():
        st = state_store.read()
        st[user] = {"son_gorulen_log": _max_id(audit.logs_store.read())}
        state_store.write(st)


def feed(user: str, limit: int = 30) -> list[dict[str, Any]]:
    """Başkalarının son işlemleri (okunmuş + okunmamış), yeniden eskiye."""
    logs = audit.logs_store.read()
    return [e for e in reversed(logs) if e["kullanici"] not in (user, "sistem")][:limit]


def describe(e: dict[str, Any], names: dict[str, str] | None = None) -> str:
    names = names if names is not None else {r["id"]: r["personel_adi"] for r in services.load_requests(include_deleted=True)}
    tid = e.get("talep_id", "-")
    who = e["kullanici"]
    p = names.get(tid)
    ref = f"{tid} · {p}" if p else (tid if tid != "-" else "")
    op = e["islem"]
    if op == "Talep Oluşturuldu":
        return f"{who} yeni talep oluşturdu: {ref}"
    if op == "Durum Değişti":
        return f"{who}: {ref} → {e['yeni_durum']}"
    if op in ("Çıktı Alındı", "İK Teslim", "SAP Girişi", "Talep Silindi", "Talep Geri Yüklendi", "Talep Düzenlendi",
              "Çıktı Geri Alındı", "İK Teslim Geri Alındı", "SAP Girişi Geri Alındı", "İK Teslim Düzeltildi"):
        return f"{who}: {op} — {ref}"
    if op == "Toplu İşlem":
        return f"{who}: toplu işlem — {e.get('detay', '')[:90]}"
    return f"{who}: {op} {ref}".strip()

# END-OF-FILE v3.1
