"""İş kuralları: arayüzden bağımsız, test edilebilir servis katmanı.

Tüm yazma işlemleri (user, role, ...) imzasını taşır, dosya kilidi altında çalışır
ve audit loguna işlenir.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import date, timedelta
from io import BytesIO
from typing import Any

import pandas as pd

from . import audit, backup, calendar_utils, config
from .config import (ACTIVE_STATUSES, ALLOWED_TRANSITIONS, ANNUAL, LEAVE_TYPES,
                     S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED)
from .storage import JsonStore, meta_store, transaction
from .utils import days_since, fmt_date, now, now_str, parse_date, safe_filename

Request = dict[str, Any]

requests_store = JsonStore(config.REQUEST_DB, list)
personnel_store = JsonStore(config.PERSONNEL_DB, list)
settings_store = JsonStore(config.SETTINGS_DB, dict)
SCHEMA_VERSION = 4


def _tx():
    """Tüm veri depolarını kapsayan işlem: hata olursa hepsi eski haline döner."""
    return transaction(requests_store, audit.logs_store, audit.anchors_store, meta_store,
                       personnel_store, settings_store, calendar_utils.holidays_store)

_REQ_DEFAULTS: dict[str, Any] = {
    "is_gunu": None, "onay_zamani": None, "karar_veren": None,
    "cikti_alindi": False, "cikti_zamani": None, "cikti_alan": None,
    "ik_teslim": False, "ik_teslim_tarihi": None, "ik_teslim_alan": None, "teslim_gecmisi": [],
    "sap_girildi": False, "sap_tarihi": None, "sap_giren": None, "sap_notu": None,
    "ekler": [],
    "silindi": False, "silinme_zamani": None, "silen": None, "silme_gerekcesi": None,
}
IMPORT_COLUMNS = ["Personel", "İzin Türü", "Başlangıç", "Bitiş", "Açıklama"]


class BalanceError(ValueError):
    """Yıllık izin bakiyesi yetersiz."""


# ----------------------------------------------------------------------------
# Yardımcılar
# ----------------------------------------------------------------------------
def _need(role: str, perm: str) -> None:
    if not config.can(role, perm):
        raise PermissionError(f"'{role}' rolünün bu işlem için yetkisi yok.")


def _need_user(user: str) -> None:
    if not user or not user.strip():
        raise ValueError("Kullanıcı adı gerekli.")


def _normalize(r: Request) -> Request:
    for k, v in _REQ_DEFAULTS.items():
        r.setdefault(k, copy.deepcopy(v))
    if r["is_gunu"] is None:
        r["is_gunu"] = calendar_utils.work_days(date.fromisoformat(r["baslangic"]), date.fromisoformat(r["bitis"]))
    return r


def load_requests(include_deleted: bool = False) -> list[Request]:
    """Varsayılan olarak silinmiş (arşivdeki) talepler dahil edilmez."""
    reqs = [_normalize(r) for r in requests_store.read()]
    return reqs if include_deleted else [r for r in reqs if not r["silindi"]]


def get_request(talep_id: str) -> Request | None:
    return next((r for r in load_requests(include_deleted=True) if r["id"] == talep_id), None)


def _find(requests: list[Request], talep_id: str, allow_deleted: bool = False) -> Request:
    for r in requests:
        if r["id"] == talep_id:
            if r.get("silindi") and not allow_deleted:
                raise ValueError("Bu talep silinmiş; işlem yapılamaz. Önce geri yükleyin.")
            return r
    raise ValueError(f"Talep bulunamadı: {talep_id}")


def _next_id(requests: list[Request]) -> str:
    year = str(now().year)
    meta = meta_store.read()
    seqs = meta.setdefault("seq", {})
    cur = seqs.get(year)
    if cur is None:
        prefix = f"IZN-{year}-"
        nums = []
        for r in requests:
            if r["id"].startswith(prefix):
                try:
                    nums.append(int(r["id"].rsplit("-", 1)[1]))
                except ValueError:
                    pass
        cur = max(nums or [0])
    cur += 1
    seqs[year] = cur
    meta_store.write(meta)
    return f"IZN-{year}-{cur:04d}"


# ----------------------------------------------------------------------------
# Ayarlar, kayıtlı filtreler
# ----------------------------------------------------------------------------
def get_settings() -> dict[str, Any]:
    return {**copy.deepcopy(config.DEFAULT_SETTINGS), **settings_store.read()}


def save_settings(user: str, role: str, new: dict[str, Any]) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        cur = get_settings()
        changed = {k: v for k, v in new.items() if k in config.DEFAULT_SETTINGS and k != "kayitli_filtreler" and cur.get(k) != v}
        if not changed:
            return
        cur.update(changed)
        settings_store.write(cur)
        audit.append_log(user, role, "-", "Ayarlar Güncellendi", detay="; ".join(f"{k}={v}" for k, v in changed.items()))


def save_filter(name: str, filt: dict[str, Any]) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Filtre adı boş olamaz.")
    with _tx():
        s = get_settings()
        s["kayitli_filtreler"][name] = filt
        settings_store.write(s)


def delete_filter(name: str) -> None:
    with _tx():
        s = get_settings()
        s["kayitli_filtreler"].pop(name, None)
        settings_store.write(s)


# ----------------------------------------------------------------------------
# Personel ve tatiller
# ----------------------------------------------------------------------------
def list_personnel(active_only: bool = False) -> list[dict[str, Any]]:
    people = personnel_store.read()
    return [p for p in people if p.get("aktif", True)] if active_only else people


def get_person(ad: str) -> dict[str, Any] | None:
    key = ad.strip().casefold()
    return next((p for p in personnel_store.read() if p["ad"].strip().casefold() == key), None)


def _add_person_unlocked(user: str, role: str, ad: str, hak: float | None = None, devreden: float = 0,
                         telefon: str = "", ekip: str = "") -> dict[str, Any]:
    people = personnel_store.read()
    if any(p["ad"].strip().casefold() == ad.strip().casefold() for p in people):
        raise ValueError(f"'{ad}' zaten kayıtlı.")
    person = {"ad": ad.strip(), "ekip": ekip.strip(), "telefon": telefon.strip(),
              "yillik_hak": hak if hak is not None else get_settings()["yillik_hak_varsayilan"],
              "devreden": 0, "hak_yillik": {},
              "devreden_yillik": {str(now().year): devreden} if devreden else {}, "aktif": True}
    people.append(person)
    personnel_store.write(people)
    audit.append_log(user, role, "-", "Personel Eklendi", detay=f"{person['ad']} | hak={person['yillik_hak']} devreden={devreden}")
    return person


def add_personnel(user: str, role: str, ad: str, hak: float | None = None, devreden: float = 0,
                  telefon: str = "", ekip: str = "") -> None:
    _need(role, "talep_gir")
    _need_user(user)
    if not ad.strip():
        raise ValueError("Personel adı boş olamaz.")
    with _tx():
        _add_person_unlocked(user, role, ad, hak, devreden, telefon, ekip)


def update_personnel(user: str, role: str, ad: str, not_: str = "", **fields: Any) -> None:
    _need(role, "admin")
    _need_user(user)
    allowed = {"ekip", "telefon", "yillik_hak", "aktif"}
    with _tx():
        people = personnel_store.read()
        p = next((x for x in people if x["ad"] == ad), None)
        if p is None:
            raise ValueError("Personel bulunamadı.")
        diffs = []
        for k, v in fields.items():
            if k in allowed and p.get(k) != v:
                diffs.append(f"{k}: {p.get(k)} → {v}")
                p[k] = v
        if diffs:
            personnel_store.write(people)
            audit.append_log(user, role, "-", "Personel Güncellendi",
                             detay=f"{ad} | " + "; ".join(diffs) + (f" | Not: {not_.strip()}" if not_.strip() else ""))


def add_holiday(user: str, role: str, tarih: date, ad: str) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        hol = calendar_utils.holidays_store.read()
        if any(h["tarih"] == tarih.isoformat() for h in hol):
            raise ValueError("Bu tarih zaten tatil olarak kayıtlı.")
        hol.append({"tarih": tarih.isoformat(), "ad": ad.strip() or "Tatil"})
        calendar_utils.holidays_store.write(sorted(hol, key=lambda h: h["tarih"]))
        audit.append_log(user, role, "-", "Tatil Eklendi", detay=f"{tarih.isoformat()} {ad}")


def remove_holiday(user: str, role: str, tarih: str) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        hol = calendar_utils.holidays_store.read()
        new = [h for h in hol if h["tarih"] != tarih]
        if len(new) == len(hol):
            raise ValueError("Tatil bulunamadı.")
        calendar_utils.holidays_store.write(new)
        audit.append_log(user, role, "-", "Tatil Silindi", detay=tarih)


# ----------------------------------------------------------------------------
# Bakiye, çakışma, yoğunluk
# ----------------------------------------------------------------------------
def find_overlap(requests: list[Request], personel: str, start: date, end: date) -> Request | None:
    key = personel.strip().casefold()
    for r in requests:
        if r["personel_adi"].strip().casefold() != key or r["durum"] not in ACTIVE_STATUSES:
            continue
        if start <= date.fromisoformat(r["bitis"]) and date.fromisoformat(r["baslangic"]) <= end:
            return r
    return None


def _year_work_days(r: Request, year: int, hol: set[date]) -> int:
    lo = max(date.fromisoformat(r["baslangic"]), date(year, 1, 1))
    hi = min(date.fromisoformat(r["bitis"]), date(year, 12, 31))
    return calendar_utils.work_days(lo, hi, hol) if lo <= hi else 0


def _year_value(p: dict[str, Any], yearly_key: str, base_key: str, year: int, default: float) -> float:
    """Yıla özel değer varsa onu, yoksa kişinin varsayılan değerini döndürür."""
    yearly = p.get(yearly_key) or {}
    return yearly[str(year)] if str(year) in yearly else p.get(base_key, default)


def balance_for(ad: str, year: int, requests: list[Request] | None = None) -> dict[str, float]:
    requests = load_requests() if requests is None else requests
    p = get_person(ad) or {}
    hol = calendar_utils.holiday_set()
    hak = _year_value(p, "hak_yillik", "yillik_hak", year, get_settings()["yillik_hak_varsayilan"])
    dev = _year_value(p, "devreden_yillik", "devreden", year, 0)
    key = ad.strip().casefold()
    used = pend = 0
    for r in requests:
        if r["personel_adi"].strip().casefold() != key or r["izin_turu"] != ANNUAL:
            continue
        if r["durum"] == S_APPROVED:
            used += _year_work_days(r, year, hol)
        elif r["durum"] == S_PENDING:
            pend += _year_work_days(r, year, hol)
    total = hak + dev
    return {"hak": hak, "devreden": dev, "kullanilan": used, "bekleyen": pend,
            "kalan": total - used, "kullanilabilir": total - used - pend}


def balance_table(year: int) -> pd.DataFrame:
    requests = load_requests()
    rows = []
    for p in list_personnel():
        b = balance_for(p["ad"], year, requests)
        rows.append({"Personel": p["ad"], "Ekip": p.get("ekip", ""), "Aktif": p.get("aktif", True),
                     "Hak": b["hak"], "Devreden": b["devreden"], "Kullanılan": b["kullanilan"],
                     "Bekleyen": b["bekleyen"], "Kalan": b["kalan"], "Kullanılabilir": b["kullanilabilir"]})
    return pd.DataFrame(rows, columns=["Personel", "Ekip", "Aktif", "Hak", "Devreden", "Kullanılan", "Bekleyen", "Kalan", "Kullanılabilir"])


def crowded_days(requests: list[Request], start: date, end: date, personel: str,
                 esik: int, hol: set[date] | None = None) -> list[tuple[date, int]]:
    hol = calendar_utils.holiday_set() if hol is None else hol
    people = calendar_utils.daily_people(requests, start, end, ACTIVE_STATUSES, exclude=personel)
    return [(d, len(s) + 1) for d, s in sorted(people.items())
            if len(s) + 1 > esik and calendar_utils.is_workday(d, hol)]


def people_on_leave(requests: list[Request], day: date, statuses: set[str] | None = None) -> list[str]:
    statuses = statuses or {S_APPROVED}
    return sorted(calendar_utils.daily_people(requests, day, day, statuses)[day])


def preview_request(personel: str, tur: str, start: date, end: date, exclude_id: str | None = None) -> dict[str, Any]:
    """Kaydetmeden önce: hata, uyarı, iş günü, bakiye ve ekip yoğunluğu."""
    out: dict[str, Any] = {"errors": [], "warnings": [], "is_gunu": 0, "takvim_gun": 0,
                            "bakiye": None, "bakiye_yetersiz": False}
    if end < start:
        out["errors"].append("Bitiş tarihi, başlangıç tarihinden önce olamaz.")
        return out
    hol = calendar_utils.holiday_set()
    out["takvim_gun"] = (end - start).days + 1
    out["is_gunu"] = calendar_utils.work_days(start, end, hol)
    if out["is_gunu"] == 0:
        out["errors"].append("Seçilen aralıkta hiç iş günü yok (hafta sonu / resmî tatil).")
    if personel.strip():
        requests = [r for r in load_requests() if r["id"] != exclude_id]
        ov = find_overlap(requests, personel, start, end)
        if ov:
            out["errors"].append(f"Aynı personelin çakışan aktif talebi var: {ov['id']} "
                                   f"({fmt_date(ov['baslangic'])} – {fmt_date(ov['bitis'])}, {ov['durum']}).")
        if tur == ANNUAL:
            b = balance_for(personel, start.year, requests)
            out["bakiye"] = b
            out["bakiye_yetersiz"] = out["is_gunu"] > b["kullanilabilir"]
        crowded = crowded_days(requests, start, end, personel, int(get_settings()["esik_ayni_anda"]), hol)
        if crowded:
            txt = ", ".join(f"{d:%d.%m} ({n} kişi)" for d, n in crowded[:6])
            out["warnings"].append(f"Ekip yoğunluğu eşiği aşılıyor: {txt}" + (" …" if len(crowded) > 6 else ""))
    return out


# ----------------------------------------------------------------------------
# Ekler
# ----------------------------------------------------------------------------
def _validate_attachment(name: str, data: bytes) -> None:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in config.ALLOWED_EXT:
        raise ValueError(f"'{name}': izin verilmeyen dosya türü. İzinli: {', '.join(sorted(config.ALLOWED_EXT))}")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"'{name}': dosya {config.MAX_UPLOAD_MB} MB sınırını aşıyor.")
    if not data:
        raise ValueError(f"'{name}': dosya boş.")


def _store_attachment(user: str, req: Request, name: str, data: bytes) -> dict[str, Any]:
    folder = config.ATTACH_DIR / req["id"]
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    stored = f"{digest[:8]}_{safe_filename(name)}"
    (folder / stored).write_bytes(data)
    meta = {"dosya": stored, "orijinal_ad": name, "boyut": len(data), "sha256": digest,
            "yukleyen": user, "zaman": now_str()}
    req["ekler"].append(meta)
    return meta


def add_attachment(user: str, role: str, talep_id: str, name: str, data: bytes) -> None:
    _need(role, "ek_ekle")
    _need_user(user)
    _validate_attachment(name, data)
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        meta = _store_attachment(user, req, name, data)
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "Ek Eklendi", detay=f"{name} | sha256={meta['sha256'][:16]}")


def read_attachment(talep_id: str, stored: str) -> bytes:
    path = config.ATTACH_DIR / talep_id / stored.split("/")[-1].split("\\")[-1]
    return path.read_bytes()


# ----------------------------------------------------------------------------
# Talep yaşam döngüsü
# ----------------------------------------------------------------------------
def create_request(user: str, role: str, personel: str, tur: str, start: date, end: date,
                   aciklama: str = "", ekler: list[tuple[str, bytes]] | None = None,
                   bakiye_override: bool = False) -> str:
    _need(role, "talep_gir")
    _need_user(user)
    personel = personel.strip()
    if not personel:
        raise ValueError("Personel adı boş olamaz.")
    if tur not in LEAVE_TYPES:
        raise ValueError("Geçersiz izin türü.")
    ekler = ekler or []
    for name, data in ekler:
        _validate_attachment(name, data)
    with _tx():
        pv = preview_request(personel, tur, start, end)
        if pv["errors"]:
            raise ValueError(pv["errors"][0])
        if pv["bakiye_yetersiz"] and not bakiye_override:
            b = pv["bakiye"]
            raise BalanceError(f"Yıllık izin bakiyesi yetersiz: kullanılabilir {b['kullanilabilir']:g} gün, talep {pv['is_gunu']} iş günü.")
        if get_person(personel) is None:
            _add_person_unlocked(user, role, personel)
        requests = [_normalize(r) for r in requests_store.read()]
        tid = _next_id(requests)
        req: Request = {
            "id": tid, "personel_adi": personel, "izin_turu": tur,
            "baslangic": start.isoformat(), "bitis": end.isoformat(),
            "gun_sayisi": pv["takvim_gun"], "is_gunu": pv["is_gunu"],
            "aciklama": aciklama.strip(), "durum": S_PENDING,
            "olusturan": user, "olusturma_zamani": now_str(),
            "son_guncelleyen": user, "son_guncelleme": now_str(),
        }
        _normalize(req)
        metas = [_store_attachment(user, req, n, d) for n, d in ekler]
        requests.append(req)
        requests_store.write(requests)
        audit.append_log(user, role, tid, "Talep Oluşturuldu", "-", S_PENDING,
                         f"{personel} | {tur} | {start.isoformat()} → {end.isoformat()} | {pv['is_gunu']} iş günü"
                         + (" | bakiye aşımı onaylandı" if pv["bakiye_yetersiz"] else ""))
        for m in metas:
            audit.append_log(user, role, tid, "Ek Eklendi", detay=f"{m['orijinal_ad']} | sha256={m['sha256'][:16]}")
    return tid


def update_status(user: str, role: str, talep_id: str, yeni: str, not_: str = "") -> None:
    _need(role, "onay")
    _need_user(user)
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        eski = req["durum"]
        if yeni not in ALLOWED_TRANSITIONS.get(eski, []):
            raise ValueError(f"'{eski}' durumundan '{yeni}' durumuna geçiş yapılamaz.")
        if yeni in (S_REJECTED, S_CANCELLED) and not not_.strip():
            raise ValueError("Reddetme ve iptal işlemlerinde gerekçe zorunludur.")
        req.update(durum=yeni, son_guncelleyen=user, son_guncelleme=now_str())
        if yeni in (S_APPROVED, S_REJECTED):
            req.update(onay_zamani=now_str(), karar_veren=user)
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "Durum Değişti", eski, yeni, not_.strip())


def mark_printed(user: str, role: str, talep_id: str) -> None:
    _need(role, "cikti")
    _need_user(user)
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if req["durum"] != S_APPROVED:
            raise ValueError("Yalnızca 'Onaylandı' durumundaki talepler için çıktı alınabilir.")
        if req["cikti_alindi"]:
            raise ValueError("Çıktı zaten alındı olarak işaretli.")
        req.update(cikti_alindi=True, cikti_zamani=now_str(), cikti_alan=user, son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "Çıktı Alındı", req["durum"], req["durum"], "İzin kağıdı çıktısı alındı")


def mark_delivered(user: str, role: str, talep_id: str, tarih: date, alan: str) -> None:
    _need(role, "teslim")
    _need_user(user)
    if not alan.strip():
        raise ValueError("Teslim alan İK personelinin adı zorunludur.")
    if tarih > now().date():
        raise ValueError("Teslim tarihi gelecekte olamaz.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if req["durum"] != S_APPROVED:
            raise ValueError("Yalnızca 'Onaylandı' durumundaki talepler için teslim işaretlenebilir.")
        if not req["cikti_alindi"]:
            raise ValueError("Önce 'çıktı alındı' adımı tamamlanmalı.")
        if req["ik_teslim"]:
            raise ValueError("Teslim zaten kayıtlı. Hata varsa 'Teslimi düzelt / geri al' kullanın.")
        req.update(ik_teslim=True, ik_teslim_tarihi=tarih.isoformat(), ik_teslim_alan=alan.strip(),
                   son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "İK Teslim", req["durum"], req["durum"],
                         f"Teslim tarihi: {tarih.isoformat()} | Teslim alan: {alan.strip()}")


def _push_delivery_history(req: Request, islem: str, user: str, gerekce: str) -> str:
    old = f"{req['ik_teslim_tarihi']} / {req['ik_teslim_alan']}"
    req["teslim_gecmisi"].append({"islem": islem, "eski_tarih": req["ik_teslim_tarihi"], "eski_alan": req["ik_teslim_alan"],
                                  "gerekce": gerekce, "kullanici": user, "zaman": now_str()})
    return old


def correct_delivery(user: str, role: str, talep_id: str, tarih: date, alan: str, gerekce: str) -> None:
    _need(role, "duzeltme")
    _need_user(user)
    if not gerekce.strip():
        raise ValueError("Düzeltme gerekçesi zorunludur.")
    if not alan.strip():
        raise ValueError("Teslim alan İK personelinin adı zorunludur.")
    if tarih > now().date():
        raise ValueError("Teslim tarihi gelecekte olamaz.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if not req["ik_teslim"]:
            raise ValueError("Bu talepte düzeltilecek bir teslim kaydı yok.")
        old = _push_delivery_history(req, "Düzeltildi", user, gerekce.strip())
        req.update(ik_teslim_tarihi=tarih.isoformat(), ik_teslim_alan=alan.strip(), son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "İK Teslim Düzeltildi", req["durum"], req["durum"],
                         f"Eski: {old} → Yeni: {tarih.isoformat()} / {alan.strip()} | Gerekçe: {gerekce.strip()}")


def revoke_delivery(user: str, role: str, talep_id: str, gerekce: str) -> None:
    _need(role, "duzeltme")
    _need_user(user)
    if not gerekce.strip():
        raise ValueError("Geri alma gerekçesi zorunludur.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if not req["ik_teslim"]:
            raise ValueError("Bu talepte geri alınacak bir teslim kaydı yok.")
        old = _push_delivery_history(req, "Geri Alındı", user, gerekce.strip())
        req.update(ik_teslim=False, ik_teslim_tarihi=None, ik_teslim_alan=None, son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "İK Teslim Geri Alındı", req["durum"], req["durum"],
                         f"Geri alınan kayıt: {old} | Gerekçe: {gerekce.strip()}")


def mark_sap(user: str, role: str, talep_id: str, tarih: date, not_: str = "") -> None:
    _need(role, "sap")
    _need_user(user)
    if tarih > now().date():
        raise ValueError("SAP giriş tarihi gelecekte olamaz.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if req["durum"] != S_APPROVED:
            raise ValueError("Yalnızca 'Onaylandı' durumundaki talepler için SAP girişi işaretlenebilir.")
        if req["sap_girildi"]:
            raise ValueError("SAP girişi zaten işaretli.")
        req.update(sap_girildi=True, sap_tarihi=tarih.isoformat(), sap_giren=user, sap_notu=not_.strip() or None,
                   son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "SAP Girişi", req["durum"], req["durum"],
                         f"SAP tarihi: {tarih.isoformat()}" + (f" | Not: {not_.strip()}" if not_.strip() else ""))


def revoke_sap(user: str, role: str, talep_id: str, gerekce: str) -> None:
    _need(role, "duzeltme")
    _need_user(user)
    if not gerekce.strip():
        raise ValueError("Geri alma gerekçesi zorunludur.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if not req["sap_girildi"]:
            raise ValueError("SAP girişi işaretli değil.")
        old = req["sap_tarihi"]
        req.update(sap_girildi=False, sap_tarihi=None, sap_giren=None, sap_notu=None, son_guncelleyen=user, son_guncelleme=now_str())
        requests_store.write(requests)
        audit.append_log(user, role, talep_id, "SAP Girişi Geri Alındı", req["durum"], req["durum"],
                         f"Geri alınan SAP tarihi: {old} | Gerekçe: {gerekce.strip()}")


def history(talep_id: str) -> list[dict[str, Any]]:
    return [e for e in audit.logs_store.read() if e.get("talep_id") == talep_id]


# ----------------------------------------------------------------------------
# Aşamalar, kuyruklar, uyarılar
# ----------------------------------------------------------------------------
def stage(r: Request) -> str:
    if r.get("silindi"):
        return "Silindi"
    if r["durum"] == S_PENDING:
        return "Onay bekliyor"
    if r["durum"] != S_APPROVED:
        return "Kapalı"
    if not r["cikti_alindi"]:
        return "Çıktı bekliyor"
    if not r["ik_teslim"]:
        return "Teslim bekliyor"
    if not r["sap_girildi"]:
        return "SAP bekliyor"
    return "Tamamlandı"


def queues(requests: list[Request]) -> dict[str, list[Request]]:
    appr = [r for r in requests if r["durum"] == S_APPROVED]
    return {
        "onay": [r for r in requests if r["durum"] == S_PENDING],
        "cikti": [r for r in appr if not r["cikti_alindi"]],
        "teslim": [r for r in appr if r["cikti_alindi"] and not r["ik_teslim"]],
        "sap": [r for r in appr if not r["sap_girildi"]],
    }


def alerts(requests: list[Request] | None = None, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    requests = load_requests() if requests is None else requests
    s = settings or get_settings()
    out: list[dict[str, Any]] = []

    def add(r: Request, kural: str, mesaj: str, age: int, esik: int, kime: str) -> None:
        out.append({"talep_id": r["id"], "personel": r["personel_adi"], "kural": kural, "mesaj": mesaj,
                    "gun": age, "seviye": "kritik" if age >= max(2 * esik, esik + 1) else "uyarı", "kime": kime})

    for r in requests:
        if r["durum"] == S_PENDING:
            age = days_since(r["olusturma_zamani"])
            if age is not None and age >= s["uyari_bekleyen_gun"]:
                add(r, "onay", f"{age} gündür onay bekliyor", age, s["uyari_bekleyen_gun"], "Yönetici")
        elif r["durum"] == S_APPROVED:
            if not r["cikti_alindi"]:
                age = days_since(r["onay_zamani"] or r["son_guncelleme"])
                if age is not None and age >= s["uyari_cikti_gun"]:
                    add(r, "cikti", f"Onaylandı, {age} gündür çıktısı alınmadı", age, s["uyari_cikti_gun"], "Planlama")
            elif not r["ik_teslim"]:
                age = days_since(r["cikti_zamani"])
                if age is not None and age >= s["uyari_teslim_gun"]:
                    add(r, "teslim", f"Çıktısı alındı, {age} gündür İK'ya teslim edilmedi", age, s["uyari_teslim_gun"], "İK")
            if not r["sap_girildi"]:
                age = days_since(r["onay_zamani"] or r["son_guncelleme"])
                if age is not None and age >= s["uyari_sap_gun"]:
                    add(r, "sap", f"Onaylandı, {age} gündür SAP girişi yapılmadı", age, s["uyari_sap_gun"], "Planlama")
    return sorted(out, key=lambda a: (a["seviye"] != "kritik", -a["gun"]))


# ----------------------------------------------------------------------------
# Tablolar, Excel içe/dışa aktarma, raporlar
# ----------------------------------------------------------------------------
REQ_COLS = ["id", "personel_adi", "izin_turu", "baslangic", "bitis", "gun_sayisi", "is_gunu", "durum", "asama",
            "cikti_alindi", "ik_teslim", "ik_teslim_tarihi", "ik_teslim_alan", "sap_girildi", "sap_tarihi",
            "ek_sayisi", "aciklama", "olusturan", "olusturma_zamani", "karar_veren", "onay_zamani", "son_guncelleme", "silindi"]


def requests_df(requests: list[Request]) -> pd.DataFrame:
    rows = []
    for r in requests:
        rows.append({
            "id": r["id"], "personel_adi": r["personel_adi"], "izin_turu": r["izin_turu"],
            "baslangic": r["baslangic"], "bitis": r["bitis"], "gun_sayisi": r["gun_sayisi"], "is_gunu": r["is_gunu"],
            "durum": r["durum"], "asama": stage(r), "cikti_alindi": bool(r["cikti_alindi"]),
            "ik_teslim": bool(r["ik_teslim"]), "ik_teslim_tarihi": r["ik_teslim_tarihi"], "ik_teslim_alan": r["ik_teslim_alan"],
            "sap_girildi": bool(r["sap_girildi"]), "sap_tarihi": r["sap_tarihi"], "ek_sayisi": len(r["ekler"]),
            "aciklama": r["aciklama"], "olusturan": r["olusturan"], "olusturma_zamani": r["olusturma_zamani"],
            "karar_veren": r["karar_veren"], "onay_zamani": r["onay_zamani"], "son_guncelleme": r["son_guncelleme"],
            "silindi": bool(r["silindi"]),
        })
    df = pd.DataFrame(rows, columns=REQ_COLS)
    for c in ["baslangic", "bitis", "ik_teslim_tarihi", "sap_tarihi", "olusturma_zamani", "onay_zamani", "son_guncelleme"]:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def monthly_usage(requests: list[Request], year: int, statuses: set[str]) -> pd.DataFrame:
    """Ay x izin türü bazında iş günü toplamı."""
    hol = calendar_utils.holiday_set()
    rows = []
    for r in requests:
        if r["durum"] not in statuses:
            continue
        s, e = date.fromisoformat(r["baslangic"]), date.fromisoformat(r["bitis"])
        for m in range(1, 13):
            lo = max(s, date(year, m, 1))
            hi = min(e, (date(year, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
            if lo <= hi:
                rows.append({"ay": m, "tur": r["izin_turu"], "gun": calendar_utils.work_days(lo, hi, hol)})
    return pd.DataFrame(rows, columns=["ay", "tur", "gun"])


def import_template_df() -> pd.DataFrame:
