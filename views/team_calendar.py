"""Ekip takvimi: kim ne zaman izinde, günlük yoğunluk ve eşik uyarısı."""
import calendar
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core import calendar_utils, config, services

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
st.title("📅 Ekip Takvimi")
today = date.today()
c1, c2, c3 = st.columns([1, 1, 2])
yil = c1.selectbox("Yıl", list(range(today.year - 1, today.year + 3)), index=1)
ay = c2.selectbox("Ay", list(range(1, 13)), index=today.month - 1, format_func=lambda m: AYLAR[m - 1])
bekleyen = c3.toggle("Onay bekleyenleri de göster", value=True)

first = date(yil, ay, 1)
last = date(yil, ay, calendar.monthrange(yil, ay)[1])
statuses = {config.S_APPROVED} | ({config.S_PENDING} if bekleyen else set())
requests = [r for r in services.load_requests() if r["durum"] in statuses
            and date.fromisoformat(r["baslangic"]) <= last and date.fromisoformat(r["bitis"]) >= first]
esik = int(services.get_settings()["esik_ayni_anda"])
hol = calendar_utils.holiday_set()

if requests:
    gantt = pd.DataFrame([{
        "Personel": r["personel_adi"], "Durum": r["durum"], "Tür": r["izin_turu"], "Talep": r["id"],
        "Başlangıç": pd.Timestamp(max(date.fromisoformat(r["baslangic"]), first)),
        "Bitiş": pd.Timestamp(min(date.fromisoformat(r["bitis"]), last) + timedelta(days=1)),
    } for r in requests])
    chart = alt.Chart(gantt).mark_bar(cornerRadius=3).encode(
        x=alt.X("Başlangıç:T", title=None, scale=alt.Scale(domain=[pd.Timestamp(first), pd.Timestamp(last + timedelta(days=1))])),
        x2="Bitiş:T", y=alt.Y("Personel:N", title=None),
        color=alt.Color("Durum:N", scale=alt.Scale(domain=[config.S_APPROVED, config.S_PENDING], range=["#2e7d32", "#f9a825"])),
        tooltip=["Talep", "Personel", "Tür", "Durum", alt.Tooltip("Başlangıç:T", format="%d.%m.%Y")],
    ).properties(height=max(120, 34 * gantt["Personel"].nunique()))
    st.altair_chart(chart, width="stretch")
else:
    st.info("Bu ayda izin kaydı yok.")

people = calendar_utils.daily_people(services.load_requests(), first, last, statuses)
daily = pd.DataFrame([{"Gün": pd.Timestamp(d), "İzinli": len(s), "Aşım": len(s) > esik and calendar_utils.is_workday(d, hol),
                       "Kişiler": ", ".join(sorted(s))} for d, s in sorted(people.items())])
bars = alt.Chart(daily).mark_bar().encode(
    x=alt.X("Gün:T", title=None), y=alt.Y("İzinli:Q", title="İzinli kişi", axis=alt.Axis(tickMinStep=1)),
    color=alt.condition("datum['Aşım']", alt.value("#c62828"), alt.value("#1565c0")),
    tooltip=[alt.Tooltip("Gün:T", format="%d.%m.%Y"), "İzinli", "Kişiler"])
rule = alt.Chart(pd.DataFrame({"y": [esik]})).mark_rule(strokeDash=[6, 4], color="#c62828").encode(y="y:Q")
st.subheader("Günlük yoğunluk")
st.altair_chart((bars + rule).properties(height=220), width="stretch")
st.caption(f"Kırmızı çizgi: aynı gün izinli kişi eşiği ({esik}). Eşik Yönetim > Ayarlar'dan değiştirilir.")

over = daily[daily["Aşım"]]
if not over.empty:
    st.warning(f"{len(over)} gün eşiği aşıyor.")
    st.dataframe(over[["Gün", "İzinli", "Kişiler"]], hide_index=True, width="stretch",
                 column_config={"Gün": st.column_config.DateColumn(format="DD.MM.YYYY")})
month_hol = [h for h in calendar_utils.holidays_store.read() if first.isoformat() <= h["tarih"] <= last.isoformat()]
if month_hol:
    st.caption("Resmî tatiller: " + " · ".join(f"{h['tarih'][8:]}.{h['tarih'][5:7]} {h['ad']}" for h in month_hol))
