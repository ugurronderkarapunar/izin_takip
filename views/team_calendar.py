"""Ekip takvimi: aylık takvim ızgarası, gün detayı ve zaman çizelgesi (Gantt)."""
import calendar
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core import calendar_utils, config, rules, services
from ui import components as ui
from ui.calendar_grid import AYLAR, legend, render_month_html

st.title("📅 Ekip Takvimi")
today = date.today()
st.session_state.setdefault("cal_y", today.year)
st.session_state.setdefault("cal_m", today.month)


def _shift(delta: int) -> None:
    m = st.session_state["cal_m"] - 1 + delta
    st.session_state["cal_y"] += m // 12
    st.session_state["cal_m"] = m % 12 + 1


def _today() -> None:
    st.session_state.update(cal_y=today.year, cal_m=today.month)


n1, n2, n3, n4, n5 = st.columns([1, 1, 1, 2, 2])
n1.button("◀ Önceki", on_click=_shift, args=(-1,), width="stretch")
n2.button("Bugün", on_click=_today, width="stretch")
n3.button("Sonraki ▶", on_click=_shift, args=(1,), width="stretch")
yil, ay = st.session_state["cal_y"], st.session_state["cal_m"]
n4.subheader(f"{AYLAR[ay - 1]} {yil}")
bekleyen = n5.toggle("Onay bekleyenleri de göster", value=True)

first = date(yil, ay, 1)
last = date(yil, ay, calendar.monthrange(yil, ay)[1])
statuses = {config.S_APPROVED} | ({config.S_PENDING} if bekleyen else set())
all_reqs = services.load_requests()
people_opts = sorted({r["personel_adi"] for r in all_reqs})
st.session_state["cal_pers"] = [p for p in st.session_state.get("cal_pers", []) if p in people_opts]
pers_f = st.multiselect("Personel filtresi", people_opts, key="cal_pers", placeholder="Tüm personel")
reqs = [r for r in all_reqs if not pers_f or r["personel_adi"] in pers_f]
esik = int(services.get_settings()["esik_ayni_anda"])
full, half = calendar_utils.holiday_info()
hol_names = {date.fromisoformat(h["tarih"]): h["ad"] for h in calendar_utils.load_holidays()}

tab_cal, tab_gantt = st.tabs(["🗓️ Takvim", "📊 Zaman çizelgesi"])
grid_start = first - timedelta(days=first.weekday())
grid_end = last + timedelta(days=6 - last.weekday())
entries = calendar_utils.daily_entries(reqs, grid_start, grid_end, statuses)

with tab_cal:
    st.markdown(render_month_html(yil, ay, entries, full, half, hol_names, esik, today), unsafe_allow_html=True)
    st.markdown(legend(), unsafe_allow_html=True)
    st.caption(f"Kırmızı çerçeve: aynı gün izinli kişi sayısı eşiği ({esik}) aştı. Eşik Yönetim > Ayarlar'dan değiştirilir.")
    st.subheader("Gün detayı")
    gun = st.date_input("Gün seçin", value=today if first <= today <= last else first, min_value=first, max_value=last,
                        format="DD.MM.YYYY", key=f"cal_gun_{yil}_{ay}")
    day_items = entries.get(gun, [])
    if day_items:
        st.dataframe(pd.DataFrame([{"Talep": r["id"], "Personel": r["personel_adi"], "Tür": r["izin_turu"], "Süre": rules.sure_etiket(r),
                                    "Durum": r["durum"]} for r in day_items]), hide_index=True, width="stretch")
        pick = st.selectbox("Talebi aç", [r["id"] for r in day_items], index=None, placeholder="Talep seçin…", key=f"cal_open_{gun}")
        if pick and st.button("Detayı aç", key="cal_open_btn"):
            ui.open_request(pick)
    else:
        st.caption(("Resmî tatil: " + hol_names[gun]) if gun in hol_names else "Bu gün izinli kişi yok.")

with tab_gantt:
    month_reqs = [r for r in reqs if r["durum"] in statuses and date.fromisoformat(r["baslangic"]) <= last
                  and date.fromisoformat(r["bitis"]) >= first]
    if month_reqs:
        gantt = pd.DataFrame([{
            "Personel": r["personel_adi"], "Durum": r["durum"], "Tür": r["izin_turu"], "Talep": r["id"],
            "Başlangıç": pd.Timestamp(max(date.fromisoformat(r["baslangic"]), first)),
            "Bitiş": pd.Timestamp(min(date.fromisoformat(r["bitis"]), last) + timedelta(days=1)),
        } for r in month_reqs])
        chart = alt.Chart(gantt).mark_bar(cornerRadius=3).encode(
            x=alt.X("Başlangıç:T", title=None, scale=alt.Scale(domain=[pd.Timestamp(first), pd.Timestamp(last + timedelta(days=1))])),
            x2="Bitiş:T", y=alt.Y("Personel:N", title=None),
            color=alt.Color("Durum:N", scale=alt.Scale(domain=[config.S_APPROVED, config.S_PENDING], range=["#2e7d32", "#f9a825"])),
            tooltip=["Talep", "Personel", "Tür", "Durum", alt.Tooltip("Başlangıç:T", format="%d.%m.%Y")],
        ).properties(height=max(120, 34 * gantt["Personel"].nunique()))
        st.altair_chart(chart, width="stretch")
    else:
        st.info("Bu ayda izin kaydı yok.")
    people = calendar_utils.daily_people(reqs, first, last, statuses, min_fraction=0.5)
    daily = pd.DataFrame([{"Gün": pd.Timestamp(d), "İzinli": len(s), "Aşım": len(s) > esik and calendar_utils.is_workday(d, full),
                           "Kişiler": ", ".join(sorted(s))} for d, s in sorted(people.items())])
    bars = alt.Chart(daily).mark_bar().encode(
        x=alt.X("Gün:T", title=None), y=alt.Y("İzinli:Q", title="İzinli kişi", axis=alt.Axis(tickMinStep=1)),
        color=alt.condition("datum['Aşım']", alt.value("#c62828"), alt.value("#1565c0")),
        tooltip=[alt.Tooltip("Gün:T", format="%d.%m.%Y"), "İzinli", "Kişiler"])
    rule = alt.Chart(pd.DataFrame({"y": [esik]})).mark_rule(strokeDash=[6, 4], color="#c62828").encode(y="y:Q")
    st.subheader("Günlük yoğunluk")
    st.altair_chart((bars + rule).properties(height=220), width="stretch")

# END-OF-FILE v3.1
