"""Evrak okuma: PDF metni -> (gerekirse) ücretsiz OCR -> evrak türü ve tarih tahmini. Saf, test edilebilir fonksiyonlar.

Sonuçlar YALNIZCA öneridir; kullanıcı onaylamadan kayıt oluşmaz (yanlış bitiş tarihi güvenlik riskidir).
"""
from __future__ import annotations

import io
import re
import shutil
from datetime import date
from functools import lru_cache
from typing import Any

TURLER = ["Gemi Adamı Cüzdanı", "Gemi Adamı Sağlık Belgesi", "Yeterlilik Belgesi", "Telsiz Belgesi", "STCW Belgesi"]
_TR = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ğ": "g", "ğ": "g", "Ş": "s", "ş": "s", "Ç": "c", "ç": "c", "Ö": "o", "ö": "o", "Ü": "u", "ü": "u"})

# (anahtar kelime, ağırlık) — Türkçe karakterden bağımsız (fold edilmiş metinde aranır)
_KW: dict[str, list[tuple[str, int]]] = {
    "Gemi Adamı Cüzdanı": [("gemi adami cuzdani", 5), ("gemiadami cuzdani", 5), ("seafarer's identity", 5), ("seafarers identity", 5),
                           ("seafarer identity", 5), ("seaman's book", 5), ("seamans book", 5), ("identity document", 3), ("cuzdan", 3)],
    "Gemi Adamı Sağlık Belgesi": [("saglik belgesi", 5), ("saglik sertifikasi", 4), ("medical certificate", 5), ("medical fitness", 4),
                                  ("fit for sea", 4), ("medical examination", 3), ("saglik", 2), ("medical", 2)],
    "Yeterlilik Belgesi": [("yeterlik belgesi", 5), ("yeterlilik belgesi", 5), ("certificate of competency", 5), ("certificate of competence", 5),
                           ("yeterlik", 3), ("yeterlilik", 3), ("competency", 2)],
    "Telsiz Belgesi": [("telsiz", 4), ("gmdss", 5), ("radio operator", 5), ("general operator", 4), ("restricted operator", 4),
                       ("radiotelephone", 3), ("radio", 2)],
    "STCW Belgesi": [("stcw", 3), ("certificate of proficiency", 4), ("basic safety", 4), ("temel guvenlik", 4), ("survival craft", 4),
                     ("can kurtarma", 4), ("fire fighting", 4), ("yangin", 3), ("ilk yardim", 3), ("first aid", 3), ("security awareness", 3),
                     ("proficiency", 2), ("egitim", 1), ("training", 1)],
}
_AYLAR = {"ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
          "oca": 1, "sub": 2, "mar": 3, "nis": 4, "haz": 6, "tem": 7, "agu": 8, "eyl": 9, "eki": 10, "kas": 11, "ara": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
          "jan": 1, "feb": 2, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_LABELS = {
    "dogum": ["dogum", "birth", "d.o.b", "dob"],
    "bitis": ["gecerlilik", "gecerli", "expiry", "expires", "expiration", "valid until", "valid to", "valid thru", "bitis", "son kullanma", "until"],
    "baslangic": ["duzenlen", "verilis", "verilme", "date of issue", "issue date", "issued", "tarihi", "veren"],
}


def fold(text: str) -> str:
    return text.translate(_TR).lower()


def classify(text: str) -> tuple[str | None, dict[str, int]]:
    """Evrak türünü tahmin eder. Belirsizse (düşük puan / yakın puan) None döner: kullanıcıya sorulur."""
    f = fold(text)
    scores = {tur: sum(w for kw, w in kws if kw in f) for tur, kws in _KW.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, second = ranked[0], ranked[1]
    if best[1] >= 3 and best[1] - second[1] >= 2:
        return best[0], scores
    return None, scores


_RX = [
    (re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)"), "dmy"),
    (re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})\s*([a-z]{3,9})\.?\s*,?\s*(\d{4})(?!\d)"), "dMy"),
]


def find_dates(text: str) -> list[dict[str, Any]]:
    """Metindeki tarihler ve yakın etiket türü (başlangıç / bitiş / doğum / None)."""
    f = fold(text)
    out: list[dict[str, Any]] = []
    for rx, kind in _RX:
        for m in rx.finditer(f):
            try:
                if kind == "dmy":
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                elif kind == "ymd":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    mon = _AYLAR.get(m.group(2))
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                continue
            if not (1990 <= d.year <= 2100):
                continue
            window = f[max(0, m.start() - 70): m.start()]
            best, pos = None, -1
            for cat, kws in _LABELS.items():
                for kw in kws:
                    i = window.rfind(kw)
                    if i > pos:
                        best, pos = cat, i
            out.append({"tarih": d, "etiket": best, "konum": m.start()})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["konum"]):
        key = (x["tarih"], x["konum"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


def suggest_dates(dates: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    """Başlangıç (düzenlenme) ve belgede yazan bitiş tarihi önerisi. Doğum tarihleri hariç tutulur."""
    today = today or date.today()
    usable = [d for d in dates if d["etiket"] != "dogum"]
    start = next((d["tarih"] for d in usable if d["etiket"] == "baslangic"), None)
    end = next((d["tarih"] for d in usable if d["etiket"] == "bitis"), None)
    if start is None:  # etiket yoksa: bugünden geçmişteki en yakın, bitiş olmayan tarih (en olası düzenlenme tarihi)
        cands = [d["tarih"] for d in usable if d["etiket"] != "bitis" and d["tarih"] <= today]
        start = max(cands) if cands else None
    return {"baslangic": start, "bitis_belgede": end, "adaylar": sorted({d["tarih"] for d in usable}, reverse=True)}


# ----------------------------------------------------------------------------- metin çıkarma
def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz as pymupdf  # eski ad
        return pymupdf


@lru_cache(maxsize=1)
def ocr_languages() -> str | None:
    """Kullanılabilir Tesseract dili ('tur+eng' / 'eng'); motor yoksa None."""
    if not shutil.which("tesseract"):
        return None
    try:
        import pytesseract
        langs = set(pytesseract.get_languages())
    except Exception:
        return None
    return "tur+eng" if {"tur", "eng"} <= langs else ("eng" if "eng" in langs else None)


def _ocr_images(images: list[Any]) -> dict[str, Any]:
    lang = ocr_languages()
    if not lang:
        return {"text": "", "yontem": "yok", "hata": "OCR motoru (Tesseract) kurulu değil: evrak bilgilerini elle girin."}
    import pytesseract
    try:
        text = "\n".join(pytesseract.image_to_string(img, lang=lang) for img in images)
    except Exception as exc:
        return {"text": "", "yontem": "yok", "hata": f"OCR çalışmadı: {exc}"}
    return {"text": text, "yontem": "ocr", "hata": None}


def extract_text(data: bytes, filename: str) -> dict[str, Any]:
    """PDF'in metin katmanını okur; taranmış (görüntü) ise ilk 3 sayfayı ücretsiz OCR (Tesseract) ile okur."""
    from PIL import Image
    try:
        if filename.lower().endswith(".pdf"):
            doc = _pymupdf().open(stream=data, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
            if len(re.sub(r"\W", "", text)) >= 40:
                return {"text": text, "yontem": "pdf-metin", "hata": None}
            images = [Image.open(io.BytesIO(p.get_pixmap(dpi=200).tobytes("png"))) for p in list(doc)[:3]]
        else:
            images = [Image.open(io.BytesIO(data))]
    except Exception as exc:
        return {"text": "", "yontem": "yok", "hata": f"Dosya açılamadı: {exc}"}
    return _ocr_images(images)


def analyze(data: bytes, filename: str, today: date | None = None) -> dict[str, Any]:
    """Dosyadan öneriler: tür (None = anlaşılamadı), başlangıç, belgede yazan bitiş, bulunan tarihler."""
    res = extract_text(data, filename)
    tur, scores = classify(res["text"]) if res["text"] else (None, {})
    sug = suggest_dates(find_dates(res["text"]), today) if res["text"] else {"baslangic": None, "bitis_belgede": None, "adaylar": []}
    return {"tur": tur, "skorlar": scores, "yontem": res["yontem"], "hata": res["hata"], **sug}

# END-OF-FILE v3.1
