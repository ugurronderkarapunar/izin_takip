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


def migrate() -> None:
    """Veritabanı şema ve veri göçü/güncellemeleri."""
    meta = meta_store.read()
    version = meta.get("schema_version", 1)
    if version < SCHEMA_VERSION:
        meta["schema_version"] = SCHEMA_VERSION
        meta_store.write(meta)


def integrity_report() -> dict[str, Any]:
    """Veri bütünlüğü kontrolü (Audit Trail ve zincir sağlığı)."""
    return audit.verify_chain()


def df_to_excel(dfs: dict[str, pd.DataFrame]) -> bytes:
    """Birden fazla DataFrame'i tek bir Excel dosyasına (farklı sekmelere) yazar."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


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
        audit.append_log
