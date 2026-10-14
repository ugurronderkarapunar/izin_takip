"""Talep listesi: filtre, kayıtlı filtreler, sayfalama, satır seçerek detay ve işlem."""
import math

import streamlit as st

from core import config, services
from ui import components as ui

st.title("📋 Talepler")
show_deleted = st.toggle("🗑️ Silinenleri göster", key="f_silinen", disabled=not ui.can("sil"),
                         help="Silinen talepler arşivdedir; geri yüklenebilir.")
requests = services.load_requests(include_deleted=show_deleted and ui.can("sil"))
settings = services.get_settings()
saved = settings["kayitli_filtreler"]
df_all = services.requests_df(requests)
if df_all.empty:
    st.info("Henüz kayıtlı talep yok.")
    st.stop()


def _apply_saved() -> None:
    f = saved.get(st.session_state.get("f_kayitli") or "", {})
    st.session_state.update(f_durum=f.get("durum", []), f_tur=f.get("tur", []),
                            f_asama=f.get("asama", []), f_ara=f.get("ara", ""))


# --- Filtreler ----------------------------------------------------------------
with st.expander("🔎 Filtreler", expanded=True):
    f1, f2, f3, f4 = st.columns(4)
    durum_f = f1.multiselect("Durum", config.ALL_STATUSES, key="f_durum")
    tur_f = f2.multiselect("İzin türü", config.LEAVE_TYPES, key="f_tur")
    asama_f = f3.multiselect("Aşama", config.STAGES, key="f_asama")
    ara = f4.text_input("Personel / ID ara", key="f_ara")
    s1, s2, s3 = st.columns([2, 2, 1])
    s1.selectbox("Kayıtlı filtre", list(saved), index=None, placeholder="Seçin…", key="f_kayitli", on_change=_apply_saved)
    yeni_ad = s2.text_input("Bu filtreyi kaydet", placeholder="Örn: Teslim bekleyenler", key="f_yeni_ad")
    if s3.button("Kaydet", key="f_kaydet", width="stretch") and yeni_ad.strip():
        services.save_filter(yeni_ad, {"durum": durum_f, "tur": tur_f, "asama": asama_f, "ara": ara})
        ui.flash(f"Filtre kaydedildi: {yeni_ad.strip()}")
        st.rerun()

df = df_all
if durum_f:
    df = df[df["durum"].isin(durum_f)]
if tur_f:
    df = df[df["izin_turu"].isin(tur_f)]
if asama_f:
    df = df[df["asama"].isin(asama_f)]
if ara.strip():
    df = df[df["personel_adi"].str.contains(ara.strip(), case=False, na=False) | df["id"].str.contains(ara.strip(), case=False, na=False)]
df = df.sort_values("olusturma_zamani", ascending=False).reset_index(drop=True)

# --- Sayfalama ----------------------------------------------------------------
p1, p2, p3 = st.columns([1, 1, 4])
size = p1.selectbox("Sayfa boyutu", [10, 25, 50, 100], key="sayfa_boyut")
pages = max(1, math.ceil(len(df) / size))
st.session_state["sayfa_no"] = min(st.session_state.get("sayfa_no", 1), pages)
page = p2.number_input("Sayfa", min_value=1, max_value=pages, step=1, key="sayfa_no")
p3.caption(f"{len(df)} kayıt · sayfa {page}/{pages}. Detay için satıra tıklayın (tekrar tıklayınca kapanır).")
view = df.iloc[(page - 1) * size: page * size].copy()
view["Durum"] = view.apply(lambda r: ui.status_label(r["durum"], r["silindi"]), axis=1)
view["Aşamalar"] = view.apply(lambda r: f"{'✅' if r['cikti_alindi'] else '⬜'} Çıktı · {'✅' if r['ik_teslim'] else '⬜'} Teslim · {'✅' if r['sap_girildi'] else '⬜'} SAP"
                              if r["durum"] == config.S_APPROVED and not r["silindi"] else "—", axis=1)

selected = st.session_state.get("secili_talep")
left, right = st.columns([5, 4]) if selected else (st.container(), None)
with left:
    event = st.dataframe(
        view[["id", "personel_adi", "izin_turu", "baslangic", "bitis", "is_gunu", "Durum", "Aşamalar", "ek_sayisi"]],
        hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key=f"liste_tablo_{st.session_state.get('tablo_nonce', 0)}",
        column_config={
            "id": "Talep ID", "personel_adi": "Personel", "izin_turu": "Tür",
            "baslangic": st.column_config.DateColumn("Başlangıç", format="DD.MM.YYYY"),
            "bitis": st.column_config.DateColumn("Bitiş", format="DD.MM.YYYY"),
            "is_gunu": st.column_config.NumberColumn("İş günü", format="%d"),
            "ek_sayisi": st.column_config.NumberColumn("Ek", format="%d"),
        },
    )
    rows = event.selection.rows
    sel_id = view.iloc[rows[0]]["id"] if rows else None
    if sel_id != st.session_state.get("_son_secim"):
        st.session_state["_son_secim"] = sel_id
        st.session_state["secili_talep"] = sel_id
        st.rerun()

    export = df.copy()
    for c in ["baslangic", "bitis", "ik_teslim_tarihi", "sap_tarihi"]:
        export[c] = export[c].dt.strftime("%d.%m.%Y")
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ CSV", export.to_csv(index=False).encode("utf-8-sig"), "talepler.csv", "text/csv", width="stretch")
    d2.download_button("⬇️ Excel", services.df_to_excel({"Talepler": export}), "talepler.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")

if right is not None:
    with right:
        with st.container(border=True):
            ui.render_detail(selected, "ls")
