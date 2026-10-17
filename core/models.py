"""Tipli veri modelleri (pydantic): yazımda doğrulama, eksik alanları tamamlama, şema geçişi."""
from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DURUMLAR = ("Beklemede", "Onaylandı", "Reddedildi", "İptal Edildi")
SURE_TIPLERI = ("tam", "yarim_sabah", "yarim_ogle", "saatlik")


def _iso_date(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    date.fromisoformat(str(v)[:10])
    return str(v)


class Ek(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dosya: str
    orijinal_ad: str
    boyut: int
    sha256: str
    yukleyen: str
    zaman: str


class TeslimGecmisi(BaseModel):
    model_config = ConfigDict(extra="forbid")
    islem: str
    eski_tarih: str | None = None
    eski_alan: str | None = None
    gerekce: str = ""
    kullanici: str
    zaman: str


class DuzenlemeGecmisi(BaseModel):
    """Düzenleme öncesi alanlar; 'geri al' bu kayıtla yapılır."""
    model_config = ConfigDict(extra="forbid")
    onceki: dict[str, Any]
    kullanici: str
    zaman: str


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    personel_adi: str
    izin_turu: str
    baslangic: str
    bitis: str
    gun_sayisi: int = 1
    is_gunu: float | None = None
    sure_tipi: Literal["tam", "yarim_sabah", "yarim_ogle", "saatlik"] = "tam"
    saat: float | None = None
    aciklama: str = ""
    durum: Literal["Beklemede", "Onaylandı", "Reddedildi", "İptal Edildi"] = "Beklemede"
    olusturan: str
    olusturma_zamani: str
    son_guncelleyen: str
    son_guncelleme: str
    onay_zamani: str | None = None
    karar_veren: str | None = None
    cikti_alindi: bool = False
    cikti_zamani: str | None = None
    cikti_alan: str | None = None
    ik_teslim: bool = False
    ik_teslim_tarihi: str | None = None
    ik_teslim_alan: str | None = None
    teslim_gecmisi: list[TeslimGecmisi] = Field(default_factory=list)
    sap_girildi: bool = False
    sap_tarihi: str | None = None
    sap_giren: str | None = None
    sap_notu: str | None = None
    ekler: list[Ek] = Field(default_factory=list)
    silindi: bool = False
    silinme_zamani: str | None = None
    silen: str | None = None
    silme_gerekcesi: str | None = None
    duzenleme_gecmisi: list[DuzenlemeGecmisi] = Field(default_factory=list)

    @field_validator("baslangic", "bitis")
    @classmethod
    def _tarih(cls, v: str) -> str:
        return _iso_date(v) or (_ for _ in ()).throw(ValueError("tarih boş olamaz"))

    @field_validator("ik_teslim_tarihi", "sap_tarihi")
    @classmethod
    def _tarih_opsiyonel(cls, v: str | None) -> str | None:
        return _iso_date(v)

    @field_validator("saat")
    @classmethod
    def _saat(cls, v: float | None) -> float | None:
        if v is not None and not (0 < v <= 24):
            raise ValueError("saat 0 ile 24 arasında olmalı")
        return v


class PersonModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ad: str
    ekip: str = ""
    telefon: str = ""
    yillik_hak: float = 20
    devreden: float = 0
    hak_yillik: dict[str, float] = Field(default_factory=dict)
    devreden_yillik: dict[str, float] = Field(default_factory=dict)
    aktif: bool = True
    ise_giris: str | None = None

    @field_validator("ise_giris")
    @classmethod
    def _giris(cls, v: str | None) -> str | None:
        return _iso_date(v)


class HolidayModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tarih: str
    ad: str
    yarim: bool = False  # arife gibi yarım gün tatil

    @field_validator("tarih")
    @classmethod
    def _t(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class KidemKademe(BaseModel):
    min_yil: int = Field(ge=0)
    hak: float = Field(ge=0, le=366)


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    yillik_hak_varsayilan: float = Field(20, ge=0, le=366)
    esik_ayni_anda: int = Field(3, ge=1, le=1000)
    uyari_bekleyen_gun: int = Field(2, ge=0, le=60)
    uyari_cikti_gun: int = Field(1, ge=0, le=60)
    uyari_teslim_gun: int = Field(2, ge=0, le=60)
    uyari_sap_gun: int = Field(3, ge=0, le=60)
    yedek_saklama_gun: int = Field(30, ge=1, le=3650)
    yonetici_telefon: str = ""
    yonetici_eposta: str = ""
    ik_telefon: str = ""
    ik_eposta: str = ""
    planlama_eposta: str = ""
    ozet_alicilari: str = ""          # virgülle ayrılmış e-posta listesi (tüm uyarıların özeti)
    bildirim_aktif: bool = False
    bildirim_saat: str = "08:30"
    uygulama_url: str = ""            # doğrulama QR kodu için (ör. https://izin.streamlit.app)
    gunluk_calisma_saati: float = Field(8, gt=0, le=24)
    otomatik_hak: bool = True
    kidem_tablosu: list[KidemKademe] = Field(default_factory=lambda: [
        KidemKademe(min_yil=1, hak=14), KidemKademe(min_yil=6, hak=20), KidemKademe(min_yil=15, hak=26)])
    kullanicilar: list[str] = Field(default_factory=list)
    kayitli_filtreler: dict[str, Any] = Field(default_factory=dict)

    @field_validator("bildirim_saat")
    @classmethod
    def _saat(cls, v: str) -> str:
        datetime.strptime(v, "%H:%M")
        return v


class LogEntryModel(BaseModel):
    """Yeni log kayıtlarının biçimi (eski kayıtlar 'rol' ve 'algo' alanlarını içermeyebilir)."""
    model_config = ConfigDict(extra="forbid")
    log_id: int
    zaman: str
    kullanici: str
    rol: str
    talep_id: str
    islem: str
    eski_durum: str = "-"
    yeni_durum: str = "-"
    detay: str = ""
    algo: str
    onceki_hash: str
    hash: str = ""


def defaults(model: type[BaseModel]) -> dict[str, Any]:
    """Zorunlu olmayan alanların varsayılanları (her çağrıda yeni kopya)."""
    out: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        if f.is_required():
            continue
        out[name] = f.default_factory() if f.default_factory else copy.deepcopy(f.default)
    return out


def normalize(model: type[BaseModel], raw: dict[str, Any]) -> dict[str, Any]:
    """Bilinmeyen alanları atar, eksikleri varsayılanla doldurur (hızlı yol; doğrulama yazımda yapılır)."""
    out = defaults(model)
    out.update({k: v for k, v in raw.items() if k in model.model_fields})
    return out


def validate_dump(model: type[BaseModel], raw: dict[str, Any]) -> dict[str, Any]:
    """Katı doğrulama: bilinmeyen alan / hatalı tip ValidationError (ValueError alt sınıfı) fırlatır."""
    return model.model_validate(raw).model_dump()

# END-OF-FILE v3.0
