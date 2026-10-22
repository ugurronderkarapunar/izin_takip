"""Denetim günlüğü: HMAC zincirli, ekleme-yalnızca kayıt + çapa (anchor) + mühür + tutarlılık kontrolü."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from . import config
from .storage import JsonStore, get_backend, meta_store, transaction
from .models import LogEntryModel
from .utils import now_str



class _AuditLog:
    """Denetim günlüğü deposu: dosyada JSONL, veritabanında yalnızca-ekleme tablo."""

    def read(self) -> list[dict[str, Any]]:
        return get_backend().audit_read()

    def last(self) -> dict[str, Any] | None:
        return get_backend().audit_last()


logs_store = _AuditLog()
anchors_store = JsonStore(config.ANCHOR_DB, list)

GENESIS = "0" * 64
ALGO = "hmac-sha256"
LEGACY_FIELDS = ["log_id", "zaman", "kullanici", "talep_id", "islem", "eski_durum", "yeni_durum", "detay"]
HMAC_FIELDS = LEGACY_FIELDS + ["rol", "algo"]
_KEY_CACHE: tuple[str, bytes] | None = None


def key_source() -> str:
    if os.environ.get("IZIN_AUDIT_KEY", "").strip():
        return "ortam değişkeni / secrets (IZIN_AUDIT_KEY)"
    return "dosya (data/.audit_key)" if get_backend().kind == "dosya" else "veritabanı (meta) — secrets'a taşımanız önerilir"


def _key() -> bytes:
    """Doğrulama anahtarı: IZIN_AUDIT_KEY > (dosya modunda) data/.audit_key > (veritabanı modunda) meta belgesi."""
    global _KEY_CACHE
    env = os.environ.get("IZIN_AUDIT_KEY", "").strip()
    if env:
        return env.encode("utf-8")
    backend = get_backend()
    tag = f"{backend.kind}:{id(backend)}"
    if _KEY_CACHE and _KEY_CACHE[0] == tag and (backend.kind != "dosya" or config.KEY_FILE.exists()):
        return _KEY_CACHE[1]
    with transaction():
        if backend.kind == "dosya":
            if not config.KEY_FILE.exists():
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                config.KEY_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
                try:
                    os.chmod(config.KEY_FILE, 0o600)
                except OSError:
                    pass
            key = config.KEY_FILE.read_text(encoding="utf-8").strip()
        else:
            meta = meta_store.read()
            if "audit_key" not in meta:
                meta["audit_key"] = secrets.token_hex(32)
                meta_store.write(meta)
            key = meta["audit_key"]
    _KEY_CACHE = (tag, key.encode("utf-8"))
    return _KEY_CACHE[1]


def _digest(entry: dict[str, Any], prev: str) -> str:
    if entry.get("algo") == ALGO:
        payload = json.dumps({k: entry[k] for k in HMAC_FIELDS}, ensure_ascii=False, sort_keys=True)
        return hmac.new(_key(), (prev + payload).encode("utf-8"), hashlib.sha256).hexdigest()
    # Eski sürümde üretilmiş (anahtarsız SHA-256) kayıtlar da doğrulanabilir
    payload = json.dumps({k: entry[k] for k in LEGACY_FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


def append_log(kullanici: str, rol: str, talep_id: str, islem: str,
               eski: str = "-", yeni: str = "-", detay: str = "") -> None:
    backend = get_backend()
    with transaction():
        last = backend.audit_last()
        prev = last["hash"] if last else GENESIS
        entry: dict[str, Any] = {
            "log_id": (last["log_id"] + 1) if last else 1, "zaman": now_str(), "kullanici": kullanici, "rol": rol,
            "talep_id": talep_id, "islem": islem, "eski_durum": eski, "yeni_durum": yeni,
            "detay": detay, "algo": ALGO, "onceki_hash": prev,
        }
        entry["hash"] = _digest(entry, prev)
        LogEntryModel.model_validate(entry)  # alan adı / tip hatalarını erken yakala
        backend.audit_append(entry)
        _maybe_anchor(entry)


def _anchor_of(entry: dict[str, Any]) -> dict[str, Any]:
    return {"tarih": now_str(), "log_id": entry["log_id"], "hash": entry["hash"]}


def _maybe_anchor(entry: dict[str, Any]) -> None:
    """Günün ilk işleminde zincirin o anki ucunu ayrı belgeye çapalar."""
    anchors = anchors_store.read()
    if anchors and anchors[-1]["tarih"][:10] == now_str()[:10]:
        return
    anchors.append(_anchor_of(entry))
    anchors_store.write(anchors)


def anchor_now() -> dict[str, Any]:
    with transaction():
        last = logs_store.last()
        if not last:
            raise ValueError("Henüz log kaydı yok.")
        anchors = anchors_store.read()
        a = _anchor_of(last)
        anchors.append(a)
        anchors_store.write(anchors)
        return a


def verify_chain(logs: list[dict[str, Any]] | None = None,
                 anchors: list[dict[str, Any]] | None = None) -> list[str]:
    """Sorun listesi döndürür; boş liste = zincir sağlam."""
    logs = logs_store.read() if logs is None else logs
    anchors = anchors_store.read() if anchors is None else anchors
    problems: list[str] = []
    prev = GENESIS
    for i, e in enumerate(logs, start=1):
        try:
            if e["log_id"] != i:
                problems.append(f"Log #{i}: sıra numarası bozuk (kayıt silinmiş/eklenmiş olabilir).")
                break
            if e["onceki_hash"] != prev:
                problems.append(f"Log #{i}: önceki kayıtla bağ kopuk.")
                break
            if not hmac.compare_digest(str(e["hash"]), _digest(e, prev)):
                problems.append(f"Log #{i}: içerik değiştirilmiş veya doğrulama anahtarı farklı.")
                break
        except KeyError:
            problems.append(f"Log #{i}: zorunlu alan eksik.")
            break
        prev = e["hash"]
    by_id = {e.get("log_id"): e for e in logs}
    for a in anchors:
        e = by_id.get(a.get("log_id"))
        if e is None:
            problems.append(f"Çapa (log #{a.get('log_id')}) için kayıt yok: log sonundan silme yapılmış olabilir.")
        elif e.get("hash") != a.get("hash"):
            problems.append(f"Çapa (log #{a.get('log_id')}) hash'i uyuşmuyor: zincir yeniden yazılmış olabilir.")
    return problems


def seal_text(logs: list[dict[str, Any]] | None = None) -> str:
    """Dışarıda (e-posta, çıktı) saklanabilecek kısa 'mühür' metni."""
    logs = logs_store.read() if logs is None else logs
    if not logs:
        return ""
    last = logs[-1]
    return f"MUHUR|{now_str()}|log_id={last['log_id']}|hash={last['hash']}"


def check_seal(seal: str, logs: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
    logs = logs_store.read() if logs is None else logs
    try:
        parts = dict(p.split("=", 1) for p in seal.strip().split("|")[2:])
        log_id, h = int(parts["log_id"]), parts["hash"]
    except (ValueError, KeyError):
        return False, "Mühür biçimi geçersiz."
    if log_id > len(logs):
        return False, f"Mühürdeki log #{log_id} artık yok: kayıtlar silinmiş."
    if logs[log_id - 1].get("hash") != h:
        return False, f"Log #{log_id} mühürle uyuşmuyor: zincir değiştirilmiş."
    return True, f"Mühür geçerli: log #{log_id} değişmemiş."


def verify_consistency(requests: list[dict[str, Any]], logs: list[dict[str, Any]] | None = None) -> list[str]:
    """request_db.json, log geçmişinden türetilen duruma uyuyor mu? (elle düzenleme tespiti)"""
    logs = logs_store.read() if logs is None else logs
    state: dict[str, dict[str, Any]] = {}
    for e in logs:
        tid = e.get("talep_id")
        if not tid or tid == "-":
            continue
        s = state.setdefault(tid, {})
        op = e.get("islem")
        if op in ("Talep Oluşturuldu", "Durum Değişti"):
            s["durum"] = e["yeni_durum"]
        elif op == "Çıktı Alındı":
            s["cikti"] = True
        elif op in ("İK Teslim", "İK Teslim Düzeltildi"):
            s["teslim"] = True
        elif op == "İK Teslim Geri Alındı":
            s["teslim"] = False
        elif op == "Çıktı Geri Alındı":
            s["cikti"] = False
        elif op == "SAP Girişi":
            s["sap"] = True
        elif op == "SAP Girişi Geri Alındı":
            s["sap"] = False
        elif op == "Talep Düzenlendi":
            s["durum"] = e["yeni_durum"]
        elif op == "Talep Silindi":
            s["silindi"] = True
        elif op == "Talep Geri Yüklendi":
            s["silindi"] = False
    issues: list[str] = []
    ids = {r["id"] for r in requests}
    for r in requests:
        s = state.get(r["id"])
        if s is None:
            issues.append(f"{r['id']}: log kaydı yok.")
            continue
        if s.get("durum") != r["durum"]:
            issues.append(f"{r['id']}: durum '{r['durum']}' ama log '{s.get('durum')}' diyor.")
        if bool(s.get("teslim")) != bool(r.get("ik_teslim")):
            issues.append(f"{r['id']}: İK teslim bilgisi log ile uyuşmuyor.")
        if bool(s.get("sap")) != bool(r.get("sap_girildi")):
            issues.append(f"{r['id']}: SAP bilgisi log ile uyuşmuyor.")
        if bool(s.get("cikti")) != bool(r.get("cikti_alindi")):
            issues.append(f"{r['id']}: çıktı bilgisi log ile uyuşmuyor.")
        if bool(s.get("silindi")) != bool(r.get("silindi")):
            issues.append(f"{r['id']}: silinme durumu log ile uyuşmuyor.")
    for tid in state:
        if tid not in ids:
            issues.append(f"{tid}: logda var ama request_db'de yok (talep silinmiş olabilir).")
    return issues

# END-OF-FILE v3.2
