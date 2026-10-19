"""İş kuralları: arayüzden bağımsız, test edilebilir servis katmanı.

Tüm yazma işlemleri (user, role, ...) imzasını taşır, dosya kilidi altında çalışır
ve audit loguna işlenir.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any

import pandas as pd

from . import audit, backup, calendar_utils, config, models, rules, storage
from .config import (ACTIVE_STATUSES, ALLOWED_TRANSITIONS, ANNUAL, LEAVE_TYPES,
                     S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED)
from .models import PersonModel, RequestModel, SettingsModel, normalize, validate_dump
from .storage import JsonStore, get_backend, meta_store, transaction
from .utils import days_since, fmt_date, now, now_str, safe_filename

Request = dict[str, Any]

requests_store = JsonStore(config.REQUEST_DB, list)
personnel_store = JsonStore(config.PERSONNEL_DB, list)
settings_store = JsonStore(config.SETTINGS_DB, dict)
SCHEMA_VERSION = 6


def _tx():
    """Tüm yazmaları kapsayan işlem: hata olursa hepsi geri alınır."""
    return transaction()

IMPORT_COLUMNS = ["Personel", "İzin Türü", "Başlangıç", "Bitiş", "Açıklama", "Birim"]


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


def _gunluk_saat() -> float:
    return float(get_settings()["gunluk_calisma_saati"])


def default_birim() -> str:
    return get_settings()["birimler"][0]["ad"]


def birim_profil(birim: str | None, settings: dict[str, Any] | None = None) -> tuple[bool, bool]:
    """(hafta sonu çalışılır mı, resmî tatil çalışılır mı). Bilinmeyen birim: ofis düzeni."""
    s = settings or get_settings()
    for b in s["birimler"]:
        if b["ad"] == birim:
            return bool(b["hafta_sonu"]), bool(b["resmi_tatil"])
    return (False, False)


def _profile_for(personel: str, birim: str | None = None) -> tuple[bool, bool]:
    """Kayıtlı personelin birimine göre; kayıtlı değilse (yeni personel) verilen / varsayılan birime göre."""
    p = get_person(personel) if personel.strip() else None
    return birim_profil(p["birim"] if p else (birim or default_birim()))


def _compute_days(sure_tipi: str, saat: float | None, start: date, end: date, profil: tuple[bool, bool] = (False, False)) -> float:
    full, half = calendar_utils.holiday_info()
    return rules.leave_days(sure_tipi, saat, start, end, full, half, _gunluk_saat(), profil)


def _normalize(r: Request) -> Request:
    """Eksik alanları varsayılanla doldurur, bilinmeyenleri atar (katı doğrulama yazımda yapılır)."""
    r = normalize(RequestModel, r)
    if r["is_gunu"] is None:
        r["is_gunu"] = _compute_days(r["sure_tipi"], r["saat"], date.fromisoformat(r["baslangic"]), date.fromisoformat(r["bitis"]),
                                     _profile_for(r["personel_adi"]))
    return r


def _save_requests(requests: list[Request]) -> None:
    """Yazmadan önce her talebi katı doğrular: yazım hatası / yanlış tip kaydı engeller (işlem geri alınır)."""
    requests_store.write([validate_dump(RequestModel, r) for r in requests])


def _save_people(people: list[dict[str, Any]]) -> None:
    personnel_store.write([validate_dump(PersonModel, p) for p in people])


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
    try:
        return SettingsModel(**settings_store.read()).model_dump()
    except ValueError:  # bozuk ayar değeri uygulamayı düşürmesin
        storage.log_event("uyarı", "ayar", "Ayarlar doğrulanamadı; varsayılanlar kullanıldı.")
        return SettingsModel().model_dump()


def save_settings(user: str, role: str, new: dict[str, Any]) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        cur = get_settings()
        changed = {k: v for k, v in new.items() if k in SettingsModel.model_fields and k != "kayitli_filtreler" and cur.get(k) != v}
        if not changed:
            return
        merged = {**cur, **changed}
        SettingsModel(**merged)  # doğrula; geçersizse ValidationError (ValueError) fırlar
        if "birimler" in changed:
            used = {p["birim"] for p in list_personnel()}
            missing = sorted(used - {b["ad"] for b in merged["birimler"]})
            if missing:
                raise ValueError("Şu birimler personelde kullanılıyor, kaldırılamaz: " + ", ".join(missing))
        settings_store.write(merged)
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
    people = [normalize(PersonModel, p) for p in personnel_store.read()]
    return [p for p in people if p["aktif"]] if active_only else people


def get_person(ad: str) -> dict[str, Any] | None:
    key = ad.strip().casefold()
    return next((p for p in list_personnel() if p["ad"].strip().casefold() == key), None)


def _add_person_unlocked(user: str, role: str, ad: str, hak: float | None = None, devreden: float = 0,
                         telefon: str = "", ekip: str = "", birim: str | None = None) -> dict[str, Any]:
    people = [normalize(PersonModel, p) for p in personnel_store.read()]
    if any(p["ad"].strip().casefold() == ad.strip().casefold() for p in people):
        raise ValueError(f"'{ad}' zaten kayıtlı.")
    birim = birim or default_birim()
    if birim not in {b["ad"] for b in get_settings()["birimler"]}:
        raise ValueError(f"Tanımsız birim: {birim}")
    person = models.defaults(PersonModel)
    person.update(ad=ad.strip(), ekip=ekip.strip(), telefon=telefon.strip(), birim=birim,
                  yillik_hak=hak if hak is not None else get_settings()["yillik_hak_varsayilan"],
                  devreden_yillik={str(now().year): devreden} if devreden else {})
    people.append(person)
    _save_people(people)
    audit.append_log(user, role, "-", "Personel Eklendi", detay=f"{person['ad']} | birim={birim} | hak={person['yillik_hak']} devreden={devreden}")
    return person


def add_personnel(user: str, role: str, ad: str, hak: float | None = None, devreden: float = 0,
                  telefon: str = "", ekip: str = "", birim: str | None = None) -> None:
    _need(role, "talep_gir")
    _need_user(user)
    if not ad.strip():
        raise ValueError("Personel adı boş olamaz.")
    with _tx():
        _add_person_unlocked(user, role, ad, hak, devreden, telefon, ekip, birim)


def update_personnel(user: str, role: str, ad: str, not_: str = "", **fields: Any) -> None:
    _need(role, "admin")
    _need_user(user)
    allowed = {"ekip", "telefon", "yillik_hak", "aktif", "ise_giris", "birim"}
    with _tx():
        people = [normalize(PersonModel, p) for p in personnel_store.read()]
        p = next((x for x in people if x["ad"] == ad), None)
        if p is None:
            raise ValueError("Personel bulunamadı.")
        diffs = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Geçersiz alan: {k}")
            if k == "birim" and v not in {b["ad"] for b in get_settings()["birimler"]}:
                raise ValueError(f"Tanımsız birim: {v}")
            if p.get(k) != v:
                diffs.append(f"{k}: {p.get(k)} → {v}")
                p[k] = v
        if diffs:
            _save_people(people)
            audit.append_log(user, role, "-", "Personel Güncellendi",
                             detay=f"{ad} | " + "; ".join(diffs) + (f" | Not: {not_.strip()}" if not_.strip() else ""))
            if "birim" in fields and any(d.startswith("birim:") for d in diffs):
                _recompute_days(user, role, ad)  # hafta sonu kuralı değişti: mevcut taleplerin iş günü yeniden hesaplanır


def _recompute_days(user: str, role: str, ad: str) -> int:
    """Personelin tam günlük taleplerinde iş gününü güncel birim kuralıyla yeniden hesaplar."""
    p = get_person(ad)
    profil = birim_profil(p["birim"] if p else None)
    full, half = calendar_utils.holiday_info()
    g = _gunluk_saat()
    requests = [_normalize(r) for r in requests_store.read()]
    n = 0
    for r in requests:
        if r["personel_adi"] != ad or r["sure_tipi"] != "tam":
            continue
        new = rules.leave_days("tam", None, date.fromisoformat(r["baslangic"]), date.fromisoformat(r["bitis"]), full, half, g, profil)
        if new != r["is_gunu"]:
            r["is_gunu"] = new
            n += 1
    if n:
        _save_requests(requests)
        audit.append_log(user, role, "-", "İş Günü Yeniden Hesaplandı", detay=f"{ad}: {n} talep (birim: {p['birim'] if p else '-'})")
    return n


def bulk_set_birim(user: str, role: str, names: list[str], birim: str) -> int:
    """Birden fazla personelin birimini tek seferde değiştirir; değişen kişi sayısını döndürür."""
    _need(role, "admin")
    _need_user(user)
    if not names:
        raise ValueError("Personel seçilmedi.")
    changed = 0
    for n in names:
        p = get_person(n)
        if p and p["birim"] != birim:
            update_personnel(user, role, n, not_="toplu birim ataması", birim=birim)
            changed += 1
    return changed


def add_holiday(user: str, role: str, tarih: date, ad: str, yarim: bool = False) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        hol = calendar_utils.load_holidays()
        if any(h["tarih"] == tarih.isoformat() for h in hol):
            raise ValueError("Bu tarih zaten tatil olarak kayıtlı.")
        hol.append({"tarih": tarih.isoformat(), "ad": ad.strip() or "Tatil", "yarim": yarim})
        calendar_utils.save_holidays(hol)
        audit.append_log(user, role, "-", "Tatil Eklendi", detay=f"{tarih.isoformat()} {ad}" + (" (yarım gün)" if yarim else ""))


def remove_holiday(user: str, role: str, tarih: str) -> None:
    _need(role, "admin")
    _need_user(user)
    with _tx():
        hol = calendar_utils.load_holidays()
        new = [h for h in hol if h["tarih"] != tarih]
        if len(new) == len(hol):
            raise ValueError("Tatil bulunamadı.")
        calendar_utils.save_holidays(new)
        audit.append_log(user, role, "-", "Tatil Silindi", detay=tarih)


# ----------------------------------------------------------------------------
# Bakiye, çakışma, yoğunluk
# ----------------------------------------------------------------------------
def find_overlap(requests: list[Request], personel: str, start: date, end: date,
                 sure_tipi: str = "tam", saat: float | None = None) -> Request | None:
    key = personel.strip().casefold()
    cand = {"baslangic": start.isoformat(), "bitis": end.isoformat(), "sure_tipi": sure_tipi, "saat": saat}
    g = _gunluk_saat()
    mine = [r for r in requests if r["personel_adi"].strip().casefold() == key and r["durum"] in ACTIVE_STATUSES]
    for r in mine:
        if rules.conflicts(r, cand, g):
            return r
    return rules.over_capacity(mine, cand, g)


def _leave_days_in_year(r: Request, year: int, full: set[date], half: set[date], profil: tuple[bool, bool] = (False, False)) -> float:
    if r.get("sure_tipi", "tam") != "tam":  # yarım gün / saatlik: tek gün
        return float(r["is_gunu"]) if date.fromisoformat(r["baslangic"]).year == year else 0.0
    lo = max(date.fromisoformat(r["baslangic"]), date(year, 1, 1))
    hi = min(date.fromisoformat(r["bitis"]), date(year, 12, 31))
    return calendar_utils.work_days(lo, hi, full, half, profil) if lo <= hi else 0.0


def _year_value(p: dict[str, Any], yearly_key: str, base_key: str, year: int, default: float) -> float:
    """Yıla özel değer varsa onu, yoksa kişinin varsayılan değerini döndürür."""
    yearly = p.get(yearly_key) or {}
    return yearly[str(year)] if str(year) in yearly else p.get(base_key, default)


def entitlement(p: dict[str, Any], year: int) -> tuple[float, str]:
    """Yıllık izin hakkı ve kaynağı: elle girilmiş > kıdemden hesaplanan > varsayılan."""
    yearly = p.get("hak_yillik") or {}
    if str(year) in yearly:
        return float(yearly[str(year)]), "elle girilmiş"
    s = get_settings()
    if s["otomatik_hak"] and p.get("ise_giris"):
        return rules.kidem_hakki(date.fromisoformat(p["ise_giris"]), year, s["kidem_tablosu"]), "kıdemden hesaplandı"
    return float(p.get("yillik_hak", s["yillik_hak_varsayilan"])), "varsayılan"


def balance_for(ad: str, year: int, requests: list[Request] | None = None) -> dict[str, float]:
    requests = load_requests() if requests is None else requests
    p = get_person(ad) or {}
    full, half = calendar_utils.holiday_info()
    hak, _ = entitlement(p, year)
    profil = birim_profil(p.get("birim"))
    dev = float(_year_value(p, "devreden_yillik", "devreden", year, 0))
    key = ad.strip().casefold()
    used = pend = 0.0
    for r in requests:
        if r["personel_adi"].strip().casefold() != key or r["izin_turu"] != ANNUAL:
            continue
        if r["durum"] == S_APPROVED:
            used += _leave_days_in_year(r, year, full, half, profil)
        elif r["durum"] == S_PENDING:
            pend += _leave_days_in_year(r, year, full, half, profil)
    total = hak + dev
    return {"hak": hak, "devreden": dev, "kullanilan": used, "bekleyen": pend,
            "kalan": total - used, "kullanilabilir": total - used - pend}


def balance_table(year: int) -> pd.DataFrame:
    requests = load_requests()
    rows = []
    for p in list_personnel():
        b = balance_for(p["ad"], year, requests)
        rows.append({"Personel": p["ad"], "Birim": p.get("birim", ""), "Ekip": p.get("ekip", ""), "Aktif": p.get("aktif", True),
                     "Hak": b["hak"], "Devreden": b["devreden"], "Kullanılan": b["kullanilan"],
                     "Bekleyen": b["bekleyen"], "Kalan": b["kalan"], "Kullanılabilir": b["kullanilabilir"]})
    return pd.DataFrame(rows, columns=["Personel", "Birim", "Ekip", "Aktif", "Hak", "Devreden", "Kullanılan", "Bekleyen", "Kalan", "Kullanılabilir"])


def crowded_days(requests: list[Request], start: date, end: date, personel: str,
                 esik: int, hol: set[date] | None = None) -> list[tuple[date, int]]:
    hol = calendar_utils.holiday_set() if hol is None else hol
    people = calendar_utils.daily_people(requests, start, end, ACTIVE_STATUSES, exclude=personel, min_fraction=0.5)
    return [(d, len(p) + 1) for d, p in sorted(people.items())
            if len(p) + 1 > esik and calendar_utils.is_workday(d, hol)]


def people_on_leave(requests: list[Request], day: date, statuses: set[str] | None = None) -> list[str]:
    statuses = statuses or {S_APPROVED}
    return sorted(calendar_utils.daily_people(requests, day, day, statuses)[day])


def preview_request(personel: str, tur: str, start: date, end: date, exclude_id: str | None = None,
                    sure_tipi: str = "tam", saat: float | None = None, birim: str | None = None) -> dict[str, Any]:
    """Kaydetmeden önce: hata, uyarı, iş günü, bakiye ve ekip yoğunluğu."""
    out: dict[str, Any] = {"errors": [], "warnings": [], "is_gunu": 0.0, "takvim_gun": 0,
                           "bakiye": None, "bakiye_yetersiz": False}
    if end < start:
        out["errors"].append("Bitiş tarihi, başlangıç tarihinden önce olamaz.")
        return out
    if sure_tipi not in models.SURE_TIPLERI:
        out["errors"].append("Geçersiz süre türü.")
        return out
    full, half = calendar_utils.holiday_info()
    out["takvim_gun"] = (end - start).days + 1
    try:
        out["is_gunu"] = rules.leave_days(sure_tipi, saat, start, end, full, half, _gunluk_saat(), _profile_for(personel, birim))
    except ValueError as exc:
        out["errors"].append(str(exc))
        return out
    if out["is_gunu"] == 0:
        out["errors"].append("Seçilen aralıkta hiç iş günü yok (hafta sonu / resmî tatil). "
                             "Gemi personeli ise kişinin birimini kontrol edin (Yönetim > Personel).")
    if personel.strip():
        requests = [r for r in load_requests() if r["id"] != exclude_id]
        ov = find_overlap(requests, personel, start, end, sure_tipi, saat)
        if ov:
            out["errors"].append(f"Aynı personelin çakışan aktif talebi var: {ov['id']} "
                                 f"({fmt_date(ov['baslangic'])} – {fmt_date(ov['bitis'])}, {rules.sure_etiket(ov)}, {ov['durum']}).")
        if tur == ANNUAL:
            b = balance_for(personel, start.year, requests)
            out["bakiye"] = b
            out["bakiye_yetersiz"] = out["is_gunu"] > b["kullanilabilir"] + 1e-9
        crowded = crowded_days(requests, start, end, personel, int(get_settings()["esik_ayni_anda"]), full)
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
    digest = hashlib.sha256(data).hexdigest()
    stored = f"{digest[:8]}_{safe_filename(name)}"
    get_backend().put_file(req["id"], stored, data)
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
        _save_requests(requests)
        audit.append_log(user, role, talep_id, "Ek Eklendi", detay=f"{name} | sha256={meta['sha256'][:16]}")


def read_attachment(talep_id: str, stored: str) -> bytes:
    return get_backend().get_file(talep_id, stored)


# ----------------------------------------------------------------------------
# Talep yaşam döngüsü
# ----------------------------------------------------------------------------
def create_request(user: str, role: str, personel: str, tur: str, start: date, end: date,
                   aciklama: str = "", ekler: list[tuple[str, bytes]] | None = None,
                   bakiye_override: bool = False, sure_tipi: str = "tam", saat: float | None = None,
                   birim: str | None = None) -> str:
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
    if birim and birim not in {b["ad"] for b in get_settings()["birimler"]}:
        raise ValueError(f"Tanımsız birim: {birim}")
    if sure_tipi != "tam":
        end = start  # yarım gün / saatlik izin tek güne girilir
    with _tx():
        pv = preview_request(personel, tur, start, end, sure_tipi=sure_tipi, saat=saat, birim=birim)
        if pv["errors"]:
            raise ValueError(pv["errors"][0])
        if pv["bakiye_yetersiz"] and not bakiye_override:
            b = pv["bakiye"]
            raise BalanceError(f"Yıllık izin bakiyesi yetersiz: kullanılabilir {b['kullanilabilir']:g} gün, talep {pv['is_gunu']:g} iş günü.")
        if get_person(personel) is None:
            _add_person_unlocked(user, role, personel, birim=birim)
        requests = [_normalize(r) for r in requests_store.read()]
        tid = _next_id(requests)
        req: Request = {
            "id": tid, "personel_adi": personel, "izin_turu": tur,
            "baslangic": start.isoformat(), "bitis": end.isoformat(),
            "gun_sayisi": pv["takvim_gun"], "is_gunu": pv["is_gunu"],
            "sure_tipi": sure_tipi, "saat": saat if sure_tipi == "saatlik" else None,
            "aciklama": aciklama.strip(), "durum": S_PENDING,
            "olusturan": user, "olusturma_zamani": now_str(),
            "son_guncelleyen": user, "son_guncelleme": now_str(),
        }
        req = _normalize(req)
        metas = [_store_attachment(user, req, n, d) for n, d in ekler]
        requests.append(req)
        _save_requests(requests)
        audit.append_log(user, role, tid, "Talep Oluşturuldu", "-", S_PENDING,
                         f"{personel} | {tur} | {start.isoformat()} → {end.isoformat()} | {rules.sure_etiket(req)} | {pv['is_gunu']:g} iş günü"
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
        _save_requests(requests)
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
        _save_requests(requests)
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
        _save_requests(requests)
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
        _save_requests(requests)
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
        _save_requests(requests)
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
        _save_requests(requests)
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
        _save_requests(requests)
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
REQ_COLS = ["id", "personel_adi", "izin_turu", "sure", "baslangic", "bitis", "gun_sayisi", "is_gunu", "durum", "asama",
            "cikti_alindi", "ik_teslim", "ik_teslim_tarihi", "ik_teslim_alan", "sap_girildi", "sap_tarihi",
            "ek_sayisi", "aciklama", "olusturan", "olusturma_zamani", "karar_veren", "onay_zamani", "son_guncelleme", "silindi"]


def requests_df(requests: list[Request]) -> pd.DataFrame:
    rows = []
    for r in requests:
        rows.append({
            "id": r["id"], "personel_adi": r["personel_adi"], "izin_turu": r["izin_turu"], "sure": rules.sure_etiket(r),
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
    full, half = calendar_utils.holiday_info()
    st_ = get_settings()
    profiles = {p["ad"]: birim_profil(p["birim"], st_) for p in list_personnel()}
    rows = []
    for r in requests:
        if r["durum"] not in statuses:
            continue
        profil = profiles.get(r["personel_adi"], (False, False))
        s0, e0 = date.fromisoformat(r["baslangic"]), date.fromisoformat(r["bitis"])
        if r.get("sure_tipi", "tam") != "tam":
            if s0.year == year:
                rows.append({"ay": s0.month, "tur": r["izin_turu"], "gun": float(r["is_gunu"])})
            continue
        for m in range(1, 13):
            lo = max(s0, date(year, m, 1))
            hi = min(e0, (date(year, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
            if lo <= hi:
                rows.append({"ay": m, "tur": r["izin_turu"], "gun": calendar_utils.work_days(lo, hi, full, half, profil)})
    return pd.DataFrame(rows, columns=["ay", "tur", "gun"])


def import_template_df() -> pd.DataFrame:
    return pd.DataFrame([{"Personel": "Ad Soyad", "İzin Türü": "Yıllık İzin", "Başlangıç": "20.10.2026",
                          "Bitiş": "22.10.2026", "Açıklama": "Örnek satır (silin)", "Birim": "Ofis"}], columns=IMPORT_COLUMNS)


def import_requests(user: str, role: str, df: pd.DataFrame) -> tuple[list[str], list[str]]:
    _need(role, "import")
    df = df.rename(columns=lambda c: str(c).strip())
    missing = [c for c in IMPORT_COLUMNS[:4] if c not in df.columns]  # "Açıklama" ve "Birim" isteğe bağlı
    if missing:
        raise ValueError("Eksik sütun(lar): " + ", ".join(missing))
    created: list[str] = []
    errors: list[str] = []
    for i, row in df.iterrows():
        try:
            start = pd.to_datetime(row["Başlangıç"], dayfirst=True).date()
            end = pd.to_datetime(row["Bitiş"], dayfirst=True).date()
            aciklama = "" if pd.isna(row.get("Açıklama")) else str(row.get("Açıklama"))
            birim = str(row["Birim"]).strip() if "Birim" in df.columns and not pd.isna(row.get("Birim")) and str(row.get("Birim")).strip() else None
            created.append(create_request(user, role, str(row["Personel"]), str(row["İzin Türü"]).strip(), start, end, aciklama, birim=birim))
        except Exception as exc:  # satır bazında hata topla
            errors.append(f"Satır {int(i) + 2}: {exc}")
    return created, errors


def export_excel() -> bytes:
    reqs = load_requests(include_deleted=True)
    df = requests_df(reqs)
    for c in ["baslangic", "bitis", "ik_teslim_tarihi", "sap_tarihi"]:
        df[c] = df[c].dt.strftime("%d.%m.%Y")
    for c in ["olusturma_zamani", "onay_zamani", "son_guncelleme"]:
        df[c] = df[c].dt.strftime("%d.%m.%Y %H:%M")
    logs = pd.DataFrame(audit.logs_store.read())
    hol = pd.DataFrame(calendar_utils.load_holidays())
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Talepler", index=False)
        balance_table(now().year).to_excel(xw, sheet_name="Bakiyeler", index=False)
        logs.to_excel(xw, sheet_name="Audit Log", index=False)
        hol.to_excel(xw, sheet_name="Tatiller", index=False)
    return buf.getvalue()


def df_to_excel(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, f in frames.items():
            f.to_excel(xw, sheet_name=name[:31], index=False)
    return buf.getvalue()


def integrity_report() -> dict[str, Any]:
    logs = audit.logs_store.read()
    return {"chain": audit.verify_chain(logs, audit.anchors_store.read()),
            "consistency": audit.verify_consistency(load_requests(include_deleted=True), logs),
            "key_source": audit.key_source(), "log_count": len(logs),
            "anchor_count": len(audit.anchors_store.read())}


# ----------------------------------------------------------------------------
# Silme (arşive kaldırma) ve geri yükleme
# ----------------------------------------------------------------------------
def delete_request(user: str, role: str, talep_id: str, gerekce: str) -> None:
    """Talebi arşive kaldırır: listelerden, bakiyeden, takvimden ve uyarılardan çıkar; kayıt ve log saklanır."""
    _need(role, "sil")
    _need_user(user)
    if not gerekce.strip():
        raise ValueError("Silme gerekçesi zorunludur.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        asama = stage(req)
        req.update(silindi=True, silinme_zamani=now_str(), silen=user, silme_gerekcesi=gerekce.strip(),
                   son_guncelleyen=user, son_guncelleme=now_str())
        _save_requests(requests)
        audit.append_log(user, role, talep_id, "Talep Silindi", req["durum"], req["durum"],
                         f"Gerekçe: {gerekce.strip()} | Silinmeden önceki aşama: {asama} | "
                         f"{req['personel_adi']} {req['baslangic']} → {req['bitis']}")


def restore_request(user: str, role: str, talep_id: str, not_: str = "") -> None:
    _need(role, "sil")
    _need_user(user)
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id, allow_deleted=True)
        if not req["silindi"]:
            raise ValueError("Bu talep silinmiş değil.")
        if req["durum"] in ACTIVE_STATUSES:
            ov = find_overlap(load_requests(), req["personel_adi"], date.fromisoformat(req["baslangic"]),
                              date.fromisoformat(req["bitis"]))
            if ov:
                raise ValueError(f"Geri yüklenemez: aynı personelin çakışan aktif talebi var ({ov['id']}).")
        req.update(silindi=False, silinme_zamani=None, silen=None, silme_gerekcesi=None,
                   son_guncelleyen=user, son_guncelleme=now_str())
        _save_requests(requests)
        audit.append_log(user, role, talep_id, "Talep Geri Yüklendi", req["durum"], req["durum"], not_.strip())


# ----------------------------------------------------------------------------
# Şema geçişi ve yedekten geri dönme
# ----------------------------------------------------------------------------
def migrate() -> None:
    """Eski sürümlerden gelen verileri yeni şemaya taşır (bir kez çalışır; hata verirse uygulamayı düşürmez)."""
    with _tx():
        meta = meta_store.read()
        ver = meta.get("schema_version", 1)
        if ver >= SCHEMA_VERSION:
            return
        raw = requests_store.read()
        normalized = [_normalize(r) for r in raw]
        try:
            _save_requests(normalized)
        except ValueError as exc:  # katı doğrulama geçmedi: normalize edilmiş hâliyle yaz, olay kaydet
            storage.log_event("uyarı", "sema", f"Talep verisi katı doğrulamadan geçmedi: {str(exc)[:300]}")
            requests_store.write(normalized)
        people = [normalize(PersonModel, p) for p in personnel_store.read()]
        for p in people:  # hak ve devreden yıl bazlı; eski tek devreden değeri bu yıla taşınır
            if p.get("devreden"):
                p["devreden_yillik"].setdefault(str(now().year), p["devreden"])
                p["devreden"] = 0
        if people:
            _save_people(people)
        calendar_utils.save_holidays(calendar_utils.load_holidays())
        added = calendar_utils.add_missing_half_days()
        if raw or people:
            audit.append_log("sistem", "Sistem", "-", "Şema Güncellendi",
                             detay=f"v{ver} → v{SCHEMA_VERSION}" + (f" | {added} arife günü eklendi" if added else ""))
        meta["schema_version"] = SCHEMA_VERSION
        meta_store.write(meta)


def restore_backup(user: str, role: str, name: str) -> None:
    _need(role, "admin")
    _need_user(user)
    backup.restore(name)
    audit.append_log(user, role, "-", "Yedekten Geri Yüklendi", detay=name)


# ----------------------------------------------------------------------------
# Düzenleme: izin hakkı (yıl bazlı), personel adı, talep
# ----------------------------------------------------------------------------
def set_year_entitlement(user: str, role: str, ad: str, year: int, hak: float, devreden: float, not_: str = "") -> None:
    """Personelin belirli bir yıl için izin hakkını ve devreden gününü elle belirler (diğer yıllar etkilenmez)."""
    _need(role, "admin")
    _need_user(user)
    if not (0 <= hak <= 366 and 0 <= devreden <= 366):
        raise ValueError("Hak ve devreden gün 0 ile 366 arasında olmalıdır.")
    with _tx():
        people = [normalize(PersonModel, p) for p in personnel_store.read()]
        p = next((x for x in people if x["ad"] == ad), None)
        if p is None:
            raise ValueError("Personel bulunamadı.")
        old_h = entitlement(p, year)[0]
        old_d = float(_year_value(p, "devreden_yillik", "devreden", year, 0))
        if (old_h, old_d) == (hak, devreden):
            return
        p["hak_yillik"][str(year)] = hak
        p["devreden_yillik"][str(year)] = devreden
        _save_people(people)
        audit.append_log(user, role, "-", "İzin Hakkı Güncellendi",
                         detay=f"{ad} | {year}: hak {old_h:g} → {hak:g}, devreden {old_d:g} → {devreden:g}"
                         + (f" | Not: {not_.strip()}" if not_.strip() else ""))


def clear_year_entitlement(user: str, role: str, ad: str, year: int) -> None:
    """Elle girilen yıl değerini kaldırır; hak yeniden kıdemden / varsayılandan hesaplanır."""
    _need(role, "admin")
    _need_user(user)
    with _tx():
        people = [normalize(PersonModel, p) for p in personnel_store.read()]
        p = next((x for x in people if x["ad"] == ad), None)
        if p is None:
            raise ValueError("Personel bulunamadı.")
        if str(year) not in p["hak_yillik"] and str(year) not in p["devreden_yillik"]:
            raise ValueError("Bu yıl için elle girilmiş değer yok.")
        p["hak_yillik"].pop(str(year), None)
        p["devreden_yillik"].pop(str(year), None)
        _save_people(people)
        audit.append_log(user, role, "-", "İzin Hakkı Güncellendi", detay=f"{ad} | {year}: elle girilen değer kaldırıldı (otomatik hesaba dönüldü)")


def rename_personnel(user: str, role: str, eski: str, yeni: str) -> None:
    """Personel adını düzeltir; o personele ait tüm taleplerdeki isim de güncellenir."""
    _need(role, "admin")
    _need_user(user)
    yeni = yeni.strip()
    if not yeni:
        raise ValueError("Yeni ad boş olamaz.")
    with _tx():
        people = [normalize(PersonModel, p) for p in personnel_store.read()]
        p = next((x for x in people if x["ad"] == eski), None)
        if p is None:
            raise ValueError("Personel bulunamadı.")
        if any(x is not p and x["ad"].strip().casefold() == yeni.casefold() for x in people):
            raise ValueError(f"'{yeni}' adında başka bir personel zaten var.")
        requests = [_normalize(r) for r in requests_store.read()]
        n = 0
        for r in requests:
            if r["personel_adi"] == eski:
                r["personel_adi"] = yeni
                n += 1
        p["ad"] = yeni
        _save_people(people)
        _save_requests(requests)
        audit.append_log(user, role, "-", "Personel Yeniden Adlandırıldı", detay=f"{eski} → {yeni} ({n} talep güncellendi)")


def can_edit_request(r: Request) -> bool:
    """Çıktısı alınmamış onaylı veya bekleyen talepler düzenlenebilir."""
    if r.get("silindi"):
        return False
    if r["durum"] == S_PENDING:
        return True
    return r["durum"] == S_APPROVED and not (r["cikti_alindi"] or r["ik_teslim"] or r["sap_girildi"])


def edit_request(user: str, role: str, talep_id: str, tur: str, start: date, end: date, aciklama: str,
                 gerekce: str = "", bakiye_override: bool = False,
                 sure_tipi: str | None = None, saat: float | None = None) -> None:
    """Tür, tarih, süre ve açıklamayı düzenler. Onaylı talepte tür/tarih/süre değişirse talep yeniden onaya düşer."""
    _need(role, "duzenle")
    _need_user(user)
    if tur not in LEAVE_TYPES:
        raise ValueError("Geçersiz izin türü.")
    with _tx():
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        if not can_edit_request(req):
            raise ValueError("Bu talep düzenlenemez (çıktısı alınmış / teslim edilmiş / SAP'ye girilmiş ya da kapanmış). "
                             "İptal edip yeni talep açın.")
        sure_tipi = sure_tipi or req["sure_tipi"]
        if sure_tipi != "tam":
            end = start
        saat = saat if sure_tipi == "saatlik" else None
        approved = req["durum"] == S_APPROVED
        old = (req["izin_turu"], req["baslangic"], req["bitis"], req["aciklama"], req["sure_tipi"], req["saat"])
        new = (tur, start.isoformat(), end.isoformat(), aciklama.strip(), sure_tipi, saat)
        if old == new:
            raise ValueError("Değişiklik yapılmadı.")
        if approved and not gerekce.strip():
            raise ValueError("Onaylı talebi düzenlemek için gerekçe zorunludur.")
        pv = preview_request(req["personel_adi"], tur, start, end, exclude_id=talep_id, sure_tipi=sure_tipi, saat=saat)
        if pv["errors"]:
            raise ValueError(pv["errors"][0])
        if pv["bakiye_yetersiz"] and not bakiye_override:
            b = pv["bakiye"]
            raise BalanceError(f"Yıllık izin bakiyesi yetersiz: kullanılabilir {b['kullanilabilir']:g} gün, talep {pv['is_gunu']:g} iş günü.")
        labels = ["Tür", "Başlangıç", "Bitiş", "Açıklama", "Süre türü", "Saat"]
        diffs = [f"{n}: {o} → {v}" for n, o, v in zip(labels, old, new) if o != v]
        material = any(old[i] != new[i] for i in (0, 1, 2, 4, 5))
        eski_durum = yeni_durum = req["durum"]
        req["duzenleme_gecmisi"].append({"onceki": {k: req[k] for k in (
            "izin_turu", "baslangic", "bitis", "aciklama", "sure_tipi", "saat", "is_gunu", "gun_sayisi", "durum", "onay_zamani", "karar_veren")},
            "kullanici": user, "zaman": now_str()})
        req.update(izin_turu=tur, baslangic=new[1], bitis=new[2], gun_sayisi=pv["takvim_gun"], is_gunu=pv["is_gunu"],
                   aciklama=new[3], sure_tipi=sure_tipi, saat=saat, son_guncelleyen=user, son_guncelleme=now_str())
        if approved and material:
            req.update(durum=S_PENDING, onay_zamani=None, karar_veren=None)
            yeni_durum = S_PENDING
            diffs.append("Onay sıfırlandı (yeniden onay gerekir)")
        _save_requests(requests)
        audit.append_log(user, role, talep_id, "Talep Düzenlendi", eski_durum, yeni_durum,
                         "; ".join(diffs) + (f" | Gerekçe: {gerekce.strip()}" if gerekce.strip() else ""))


# ----------------------------------------------------------------------------
# Toplu işlemler
# ----------------------------------------------------------------------------
def bulk_eligible(r: Request, action: str) -> bool:
    """Talep, seçilen toplu işleme uygun durumda mı?"""
    if r.get("silindi"):
        return False
    if action in ("onayla", "reddet"):
        return r["durum"] == S_PENDING
    if action == "iptal":
        return r["durum"] in ACTIVE_STATUSES
    if r["durum"] != S_APPROVED:
        return False
    if action == "cikti":
        return not r["cikti_alindi"]
    if action == "teslim":
        return bool(r["cikti_alindi"]) and not r["ik_teslim"]
    if action == "sap":
        return not r["sap_girildi"]
    return False


def bulk_apply(user: str, role: str, ids: list[str], action: str, not_: str = "",
               tarih: date | None = None, alan: str = "") -> dict[str, Any]:
    """Seçili taleplere aynı işlemi uygular. Her talep ayrı işlemdir: biri başarısız olursa diğerleri etkilenmez.

    Döner: {"ok": [id, ...], "failed": {id: hata}}. Ayrıca özet bir 'Toplu İşlem' log kaydı düşülür.
    """
    if action not in config.BULK_ACTIONS:
        raise ValueError("Geçersiz toplu işlem.")
    label, perm = config.BULK_ACTIONS[action]
    _need(role, perm)
    _need_user(user)
    ids = list(dict.fromkeys(ids))  # tekrarları at, sırayı koru
    if not ids:
        raise ValueError("İşlem yapılacak talep seçilmedi.")
    if action in ("reddet", "iptal") and not not_.strip():
        raise ValueError("Reddetme ve iptal işlemlerinde gerekçe zorunludur.")
    if action == "teslim" and (tarih is None or not alan.strip()):
        raise ValueError("Teslim tarihi ve teslim alan İK personeli zorunludur.")
    if action == "sap" and tarih is None:
        raise ValueError("SAP giriş tarihi zorunludur.")
    note = (f"{not_.strip()} " if not_.strip() else "") + f"[Toplu işlem: {len(ids)} talep]"
    ok: list[str] = []
    failed: dict[str, str] = {}
    for tid in ids:
        try:
            r = get_request(tid)
            if r is None or not bulk_eligible(r, action):
                raise ValueError("Bu işlem için uygun durumda değil.")
            if action == "onayla":
                update_status(user, role, tid, S_APPROVED, note)
            elif action == "reddet":
                update_status(user, role, tid, S_REJECTED, note)
            elif action == "iptal":
                update_status(user, role, tid, S_CANCELLED, note)
            elif action == "cikti":
                mark_printed(user, role, tid)
            elif action == "teslim":
                mark_delivered(user, role, tid, tarih, alan)
            elif action == "sap":
                mark_sap(user, role, tid, tarih, note)
            ok.append(tid)
        except (ValueError, PermissionError) as exc:
            failed[tid] = str(exc)
    with _tx():
        audit.append_log(user, role, "-", "Toplu İşlem",
                         detay=f"{label.split(' ', 1)[-1]}: {len(ok)} başarılı, {len(failed)} hatalı | Başarılı: {', '.join(ok) or '-'}"
                         + (f" | Hatalı: {', '.join(failed)}" if failed else ""))
    return {"ok": ok, "failed": failed}



# ----------------------------------------------------------------------------
# Geri alma (undo): kendi başına yapılan son işlemi, süre dolmadıysa ve sonradan başka işlem yapılmadıysa geri alır
# ----------------------------------------------------------------------------
UNDO_WINDOW_MIN = 60
_UNDO_PERM = {"Talep Oluşturuldu": "talep_gir", "Durum Değişti": "onay", "Çıktı Alındı": "cikti", "İK Teslim": "teslim",
              "SAP Girişi": "sap", "Talep Silindi": "sil", "Talep Düzenlendi": "duzenle"}


def _age_minutes(zaman: str) -> float:
    try:
        t = datetime.strptime(zaman, "%Y-%m-%d %H:%M:%S").replace(tzinfo=config.TZ)
    except ValueError:
        return 1e9
    return (now() - t).total_seconds() / 60


def recent_undoable(limit: int = 6, window_min: int = UNDO_WINDOW_MIN) -> list[dict[str, Any]]:
    """Talep başına son log kaydı geri alınabilir türdeyse ve süre dolmadıysa listeler (en yeniden eskiye)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in reversed(audit.logs_store.read()[-400:]):
        tid = e.get("talep_id")
        if not tid or tid == "-" or tid in seen:
            continue
        seen.add(tid)
        if e["islem"] not in _UNDO_PERM or str(e.get("detay", "")).startswith("↩️"):
            continue
        if _age_minutes(e["zaman"]) > window_min:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def undo_last(user: str, role: str, talep_id: str) -> str:
    """Talepteki son işlemi geri alır; geri alınan işlemin adını döndürür. Geri alma da loglanır."""
    _need_user(user)
    with _tx():
        cand = next((e for e in recent_undoable(limit=1000) if e["talep_id"] == talep_id), None)
        if cand is None:
            raise ValueError("Bu talepte geri alınabilir bir son işlem yok (süre dolmuş ya da sonradan başka işlem yapılmış).")
        op = cand["islem"]
        _need(role, _UNDO_PERM[op])
        label = f"↩️ Geri alındı: {op} (log #{cand['log_id']}, {cand['kullanici']})"
        if op == "Talep Oluşturuldu":
            delete_request(user, role, talep_id, label)
            return op
        if op == "Talep Silindi":
            restore_request(user, role, talep_id, label)
            return op
        requests = [_normalize(r) for r in requests_store.read()]
        req = _find(requests, talep_id)
        stamp = {"son_guncelleyen": user, "son_guncelleme": now_str()}
        if op == "Durum Değişti":
            if req["durum"] != cand["yeni_durum"]:
                raise ValueError("Talep durumu sonradan değişmiş; geri alınamaz.")
            eski = cand["eski_durum"]
            req.update(durum=eski, **stamp)
            if eski == S_PENDING:
                req.update(onay_zamani=None, karar_veren=None)
            _save_requests(requests)
            audit.append_log(user, role, talep_id, "Durum Değişti", cand["yeni_durum"], eski, label)
        elif op == "Çıktı Alındı":
            if not req["cikti_alindi"] or req["ik_teslim"]:
                raise ValueError("Çıktı durumu sonradan değişmiş; geri alınamaz.")
            req.update(cikti_alindi=False, cikti_zamani=None, cikti_alan=None, **stamp)
            _save_requests(requests)
            audit.append_log(user, role, talep_id, "Çıktı Geri Alındı", req["durum"], req["durum"], label)
        elif op == "İK Teslim":
            if not req["ik_teslim"]:
                raise ValueError("Teslim kaydı sonradan değişmiş; geri alınamaz.")
            _push_delivery_history(req, "Geri Alındı", user, label)
            req.update(ik_teslim=False, ik_teslim_tarihi=None, ik_teslim_alan=None, **stamp)
            _save_requests(requests)
            audit.append_log(user, role, talep_id, "İK Teslim Geri Alındı", req["durum"], req["durum"], label)
        elif op == "SAP Girişi":
            if not req["sap_girildi"]:
                raise ValueError("SAP kaydı sonradan değişmiş; geri alınamaz.")
            req.update(sap_girildi=False, sap_tarihi=None, sap_giren=None, sap_notu=None, **stamp)
            _save_requests(requests)
            audit.append_log(user, role, talep_id, "SAP Girişi Geri Alındı", req["durum"], req["durum"], label)
        elif op == "Talep Düzenlendi":
            if not req["duzenleme_gecmisi"]:
                raise ValueError("Düzenleme geçmişi bulunamadı; geri alınamaz.")
            snap = req["duzenleme_gecmisi"].pop()["onceki"]
            pv = preview_request(req["personel_adi"], snap["izin_turu"], date.fromisoformat(snap["baslangic"]),
                                 date.fromisoformat(snap["bitis"]), exclude_id=talep_id,
                                 sure_tipi=snap.get("sure_tipi", "tam"), saat=snap.get("saat"))
            if pv["errors"]:
                raise ValueError("Eski değerler artık geçerli değil: " + pv["errors"][0])
            eski_durum = req["durum"]
            req.update(snap)
            req.update(**stamp)
            _save_requests(requests)
            audit.append_log(user, role, talep_id, "Talep Düzenlendi", eski_durum, req["durum"], label)
    return op

# END-OF-FILE v3.1
