"""Aylık takvim ızgarası (saf HTML üretimi; Streamlit'ten bağımsız, test edilebilir)."""
from __future__ import annotations

import calendar
from datetime import date
from html import escape
from typing import Any

from core import rules

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]

CSS = (
    "<style>"
    ".izn-cal{width:100%;border-collapse:collapse;table-layout:fixed}"
    ".izn-cal th{padding:6px;text-align:center;font-size:.8rem;opacity:.7}"
    ".izn-cal td{vertical-align:top;height:108px;border:1px solid rgba(128,128,128,.35);padding:4px;font-size:.74rem;overflow:hidden}"
    ".izn-d{font-weight:600;font-size:.82rem;display:flex;justify-content:space-between}"
    ".izn-cnt{font-weight:400;opacity:.75}"
    ".izn-out{opacity:.35}.izn-we{background:rgba(128,128,128,.10)}.izn-hol{background:rgba(198,40,40,.12)}"
    ".izn-half{background:rgba(249,168,37,.12)}.izn-today{outline:2px solid #1565c0;outline-offset:-2px}"
    ".izn-over{box-shadow:inset 0 0 0 2px #c62828}"
    ".izn-chip{display:block;margin:2px 0;padding:1px 5px;border-radius:4px;white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis;color:#fff}.izn-ok{background:#2e7d32}.izn-wait{background:#b26a00}"
    ".izn-more{opacity:.7}.izn-note{font-size:.66rem;opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".izn-leg span{display:inline-block;margin-right:14px;font-size:.8rem}"
    "</style>"
)


def _chip(r: dict[str, Any]) -> str:
    tip = r.get("sure_tipi", "tam")
    prefix = "½ " if tip.startswith("yarim") else ("⏱ " if tip == "saatlik" else "")
    cls = "izn-ok" if r["durum"] == "Onaylandı" else "izn-wait"
    tooltip = escape(f"{r['personel_adi']} · {r['izin_turu']} · {rules.sure_etiket(r)} · {r['id']} · {r['durum']}", quote=True)
    return f"<span class='izn-chip {cls}' title='{tooltip}'>{prefix}{escape(r['personel_adi'])}</span>"


def legend() -> str:
    return ("<div class='izn-leg'><span><b style='color:#2e7d32'>■</b> Onaylı</span><span><b style='color:#b26a00'>■</b> Onay bekliyor</span>"
            "<span>½ yarım gün</span><span>⏱ saatlik</span><span><b style='color:#c62828'>▢</b> yoğunluk eşiği aşıldı</span>"
            "<span><b style='color:#c62828'>■</b> resmî tatil</span><span><b style='color:#f9a825'>■</b> arife (yarım gün)</span></div>")


def render_month_html(year: int, month: int, entries: dict[date, list[dict[str, Any]]], full: set[date], half: set[date],
                      holiday_names: dict[date, str], esik: int, today: date | None = None, max_chips: int = 3) -> str:
    """Aylık takvimi tablo olarak üretir. entries: gün -> o güne denk gelen talepler (yalnızca ilgili ay dışı günler boş olabilir)."""
    today = today or date.today()
    rows = ["<table class='izn-cal'><thead><tr>" + "".join(f"<th>{g}</th>" for g in GUNLER) + "</tr></thead><tbody>"]
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        rows.append("<tr>")
        for d in week:
            items = sorted(entries.get(d, []), key=lambda r: (r["durum"] != "Onaylandı", r["personel_adi"]))
            people = {r["personel_adi"] for r in items if rules.fraction(r.get("sure_tipi", "tam"), r.get("saat")) >= 0.5}
            workday = d.weekday() not in (5, 6) and d not in full
            cls = ["izn-out"] if d.month != month else []
            if d.weekday() in (5, 6):
                cls.append("izn-we")
            if d in full:
                cls.append("izn-hol")
            elif d in half:
                cls.append("izn-half")
            if d == today:
                cls.append("izn-today")
            if workday and len(people) > esik:
                cls.append("izn-over")
            cnt = f"<span class='izn-cnt'>{len(people)} kişi</span>" if people else ""
            chips = "".join(_chip(r) for r in items[:max_chips])
            more = f"<div class='izn-more'>+{len(items) - max_chips} daha</div>" if len(items) > max_chips else ""
            note = f"<div class='izn-note'>{escape(holiday_names[d])}</div>" if d in holiday_names and d.month == month else ""
            rows.append(f"<td class='{' '.join(cls)}'><div class='izn-d'><span>{d.day}</span>{cnt}</div>{note}{chips}{more}</td>")
        rows.append("</tr>")
    rows.append("</tbody></table>")
    return CSS + "".join(rows)

# END-OF-FILE v3.0
