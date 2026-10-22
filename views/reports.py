"""Raporlar, Excel dışa/içe aktarma."""
from datetime import date

import pandas as pd
import streamlit as st

from core import config, services
from ui import components as ui

AYLAR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.title("📊 Raporlar")
tab_rapor, tab_excel = st.tabs(["Özet raporlar", "Excel içe / dışa aktar"])

with tab_rapor:
    requests = services.load_requests()
    if not requests:
        st.info("Rapor için henüz veri yok.")
    else:
        yil = st.selectbox("Yıl", list(range(date.today().year - 1, date.today().year + 2)), index=1)
        df = services.requests_df(requests)
        yr = df[(df["baslangic"].dt.year == yil) | (df["bitis"].dt.year == yil)]
        onayli = yr[yr["durum"] == config.S_APPROVED]
        onay_sure = (yr["onay_zamani"] - yr["olusturma_zamani"]).dropna()
        teslim_sure = (onayli["ik_teslim_tarihi"] - onayli["onay_zamani"].dt.normalize()).dropna()
        tamam = (yr["asama"] == "Tamamlandı").sum()

        def _fmt(td: pd.Timedelta) -> str:
            h = td.total_seconds() / 3600
            return f"{h:.1f} saat" if h < 48 else f"{h / 24:.1f} gün"

        m = st.columns(5)
        m[0].metric("Talep sayısı", len(yr))
        m[1].metric("Onaylı izin (iş günü)", f"{onayli['is_gunu'].sum():g}")
        m[2].metric("Ort. onay süresi", _fmt(onay_sure.mean()) if len(onay_sure) else "—")
        m[3].metric("Onay → İK teslim", f"{teslim_sure.dt.days.mean():.1f} gün" if len(teslim_sure) else "—")
        m[4].metric("Süreci tamamlanan", f"{tamam}/{len(onayli)}" if len(onayli) else "—")

        st.subheader("Aylık izin kullanımı (iş günü)")
        mu = services.monthly_usage(requests, yil, {config.S_APPROVED})
        if mu.empty:
            st.caption("Onaylı izin yok.")
        else:
            pv = mu.pivot_table(index="ay", columns="tur", values="gun", aggfunc="sum", fill_value=0)
            pv = pv.reindex(range(1, 13), fill_value=0)
            pv.index = [f"{m_:02d} {AYLAR[m_ - 1]}" for m_ in pv.index]
            st.bar_chart(pv)

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Türe göre talep")
            st.bar_chart(yr["izin_turu"].value_counts())
        with c2:
            st.subheader("Duruma göre talep")
            st.bar_chart(yr["durum"].value_counts())
        st.subheader("En çok izin kullanan 10 personel (iş günü)")
        top = onayli.groupby("personel_adi")["is_gunu"].sum().sort_values(ascending=False).head(10)
        if len(top):
            st.bar_chart(top)

with tab_excel:
    st.subheader("Dışa aktar")
    st.download_button("⬇️ Tüm veriyi Excel'e aktar (Talepler, Bakiyeler, Audit Log, Tatiller)", services.export_excel(),
                       f"izin_takip_{date.today().isoformat()}.xlsx", XLSX)
    st.subheader("İçe aktar (toplu talep girişi)")
    if not ui.can("import"):
        st.info("Toplu içe aktarma yetkisi 'Planlama' ve 'Admin' rollerindedir.")
    else:
        st.download_button("Şablonu indir", services.df_to_excel({"Talepler": services.import_template_df()}), "izin_sablon.xlsx", XLSX)
        up = st.file_uploader("Excel / CSV dosyası", type=["xlsx", "csv"], key="imp_file")
        if up is not None:
            try:
                src = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
            except Exception as exc:  # okunamayan dosya
                st.error(f"Dosya okunamadı: {exc}")
            else:
                st.dataframe(src.head(50), hide_index=True, width="stretch")
                st.caption(f"{len(src)} satır. Talepler 'Beklemede' olarak açılır; hatalı satırlar atlanır ve listelenir.")
                if st.button("İçe aktar", type="primary", key="imp_go") and ui.user_ok():
                    try:
                        created, errors = services.import_requests(ui.current_user(), ui.current_role(), src)
                    except (ValueError, PermissionError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"{len(created)} talep oluşturuldu.")
                        if errors:
                            st.error("Atlanan satırlar:\n\n" + "\n".join(f"- {e}" for e in errors))

# END-OF-FILE v3.2
