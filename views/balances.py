"""Yıllık izin bakiyeleri."""
from datetime import date

import streamlit as st

from core import services

st.title("🧮 İzin Bakiyeleri")
yil = st.selectbox("Yıl", list(range(date.today().year - 1, date.today().year + 2)), index=1)
df = services.balance_table(yil)
if df.empty:
    st.info("Henüz personel kaydı yok. Personel, ilk talep girildiğinde otomatik eklenir (Yönetim sayfasından da eklenebilir).")
    st.stop()
only_active = st.toggle("Sadece aktif personel", value=True)
if only_active:
    df = df[df["Aktif"]]
df = df.drop(columns=["Aktif"]).copy()
toplam = (df["Hak"] + df["Devreden"]).replace(0, 1)
df["Kullanım %"] = (df["Kullanılan"] / toplam * 100).round(0)

m = st.columns(3)
m[0].metric("Personel", len(df))
m[1].metric("Toplam kullanılan (iş günü)", f"{df['Kullanılan'].sum():g}")
m[2].metric("Bakiyesi eksiye düşen", int((df["Kullanılabilir"] < 0).sum()))
st.dataframe(df, hide_index=True, width="stretch", column_config={
    "Kullanım %": st.column_config.ProgressColumn("Kullanım", min_value=0, max_value=100, format="%d%%"),
    "Kullanılabilir": st.column_config.NumberColumn(help="Kalan bakiye − onay bekleyen yıllık izinler"),
})
st.download_button("⬇️ Excel", services.df_to_excel({f"Bakiye {yil}": df}), f"bakiyeler_{yil}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
st.caption("Hak ve devreden değerleri Yönetim > Personel sayfasından, yıl bazında düzenlenir. Bakiye yalnızca 'Yıllık İzin' türünü kapsar; hesap iş günü üzerinden yapılır (hafta sonu ve resmî tatiller düşülür).")

# END-OF-FILE v3.1
