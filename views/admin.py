"""Yönetim: personel, ayarlar, tatiller, yedek, güvenlik."""
from datetime import date

import pandas as pd
import streamlit as st

from core import audit, backup, calendar_utils, services
from ui import components as ui

st.title("⚙️ Yönetim")
if not ui.can("admin"):
    st.warning("Bu sayfa yalnızca 'Admin' rolü içindir.")
    st.stop()

t_per, t_set, t_hol, t_bak, t_sec = st.tabs(["👥 Personel", "🎛️ Ayarlar", "🎌 Tatiller", "💾 Yedek", "🔐 Güvenlik"])

with t_per:
    people = services.list_personnel()
    if people:
        df = pd.DataFrame(people).reindex(columns=["ad", "ekip", "telefon", "yillik_hak", "devreden", "aktif"])
        df[["ekip", "telefon"]] = df[["ekip", "telefon"]].fillna("")
        edited = st.data_editor(df, hide_index=True, width="stretch", num_rows="fixed", disabled=["ad"], key="pers_editor",
                                column_config={"ad": "Personel", "ekip": "Ekip", "telefon": "Telefon",
                                               "yillik_hak": st.column_config.NumberColumn("Yıllık hak", min_value=0, step=1),
                                               "devreden": st.column_config.NumberColumn("Devreden", min_value=0, step=1),
                                               "aktif": st.column_config.CheckboxColumn("Aktif")})
        st.caption("Personel silinmez; ayrılanlar 'Aktif' işaretinin kaldırılmasıyla pasife alınır. Değişiklikler loglanır.")
        if st.button("Değişiklikleri kaydet", key="pers_save", type="primary"):
            n = 0
            for i, row in edited.iterrows():
                orig = df.loc[i]
                fields = {c: (bool(row[c]) if c == "aktif" else (str(row[c]) if c in ("ekip", "telefon") else float(row[c])))
                          for c in ["ekip", "telefon", "yillik_hak", "devreden", "aktif"] if row[c] != orig[c]}
                if fields and ui.run(services.update_personnel, row["ad"], ok_msg="Personel güncellendi", **fields):
                    n += 1
            if n:
                st.rerun()
    with st.form("pers_add", clear_on_submit=False):
        st.markdown("**Yeni personel ekle**")
        a1, a2, a3, a4, a5 = st.columns(5)
        ad = a1.text_input("Ad soyad *")
        ekip = a2.text_input("Ekip")
        tel = a3.text_input("Telefon")
        hak = a4.number_input("Yıllık hak", 0, 60, int(services.get_settings()["yillik_hak_varsayilan"]))
        dev = a5.number_input("Devreden", 0, 60, 0)
        if st.form_submit_button("Ekle") and ui.run(services.add_personnel, ad, hak, dev, tel, ekip, ok_msg=f"{ad} eklendi"):
            st.rerun()

with t_set:
    s = services.get_settings()
    with st.form("ayarlar"):
        st.markdown("**Süreç eşikleri**")
        a1, a2, a3, a4 = st.columns(4)
        hak = a1.number_input("Varsayılan yıllık hak", 0, 60, int(s["yillik_hak_varsayilan"]))
        esik = a2.number_input("Aynı gün izinli eşiği", 1, 100, int(s["esik_ayni_anda"]))
        yedek = a3.number_input("Yedek saklama (gün)", 1, 365, int(s["yedek_saklama_gun"]))
        b1, b2, b3, b4 = st.columns(4)
        u1 = b1.number_input("Onay bekleme uyarısı (gün)", 0, 30, int(s["uyari_bekleyen_gun"]))
        u2 = b2.number_input("Çıktı alınmama uyarısı (gün)", 0, 30, int(s["uyari_cikti_gun"]))
        u3 = b3.number_input("Teslim edilmeme uyarısı (gün)", 0, 30, int(s["uyari_teslim_gun"]))
        u4 = b4.number_input("SAP girişi uyarısı (gün)", 0, 30, int(s["uyari_sap_gun"]))
        st.markdown("**Bildirim kişileri** (uyarılardaki WhatsApp / e-posta bağlantıları için)")
        c1, c2, c3, c4 = st.columns(4)
        yt = c1.text_input("Yönetici telefon", s["yonetici_telefon"])
        ye = c2.text_input("Yönetici e-posta", s["yonetici_eposta"])
        it = c3.text_input("İK telefon", s["ik_telefon"])
        ie = c4.text_input("İK e-posta", s["ik_eposta"])
        if st.form_submit_button("Ayarları kaydet", type="primary"):
            new = {"yillik_hak_varsayilan": hak, "esik_ayni_anda": esik, "yedek_saklama_gun": yedek,
                   "uyari_bekleyen_gun": u1, "uyari_cikti_gun": u2, "uyari_teslim_gun": u3, "uyari_sap_gun": u4,
                   "yonetici_telefon": yt, "yonetici_eposta": ye, "ik_telefon": it, "ik_eposta": ie}
            if ui.run(services.save_settings, new, ok_msg="Ayarlar kaydedildi"):
                st.rerun()

with t_hol:
    st.caption("Dinî bayram tarihleri her yıl değişir; ilgili yılın Diyanet takvimiyle kontrol edip güncelleyin.")
    hol = calendar_utils.holidays_store.read()
    st.dataframe(pd.DataFrame(hol).rename(columns={"tarih": "Tarih", "ad": "Tatil"}), hide_index=True, width="stretch", height=260)
    with st.form("hol_add"):
        h1, h2 = st.columns(2)
        hd = h1.date_input("Tarih", value=date.today(), format="DD.MM.YYYY")
        hn = h2.text_input("Tatil adı")
        if st.form_submit_button("Tatil ekle") and ui.run(services.add_holiday, hd, hn, ok_msg="Tatil eklendi"):
            st.rerun()
    rm = st.selectbox("Tatil kaldır", [h["tarih"] + " · " + h["ad"] for h in hol], index=None, placeholder="Seçin…")
    if rm and st.button("Seçili tatili kaldır"):
        if ui.run(services.remove_holiday, rm.split(" · ")[0], ok_msg="Tatil kaldırıldı"):
            st.rerun()

with t_bak:
    st.caption("Her gün ilk açılışta otomatik yedek alınır. Yedekler data/backups klasöründedir.")
    b1, b2 = st.columns(2)
    if b1.button("Şimdi yedek al", width="stretch"):
        d = backup.backup_now("manuel")
        st.success(f"Yedek alındı: {d.name}")
    b2.download_button("⬇️ Tüm veriyi ZIP indir (ekler dahil)", backup.zip_bytes(), f"izin_takip_yedek_{date.today().isoformat()}.zip",
                       "application/zip", width="stretch")
    st.write(f"Mevcut yedekler ({len(backup.list_backups())}):")
    st.code("\n".join(backup.list_backups()[:30]) or "—", language=None)

with t_sec:
    rep = services.integrity_report()
    st.write(f"Doğrulama anahtarı kaynağı: **{rep['key_source']}**")
    st.caption("Anahtar, log zincirini HMAC ile imzalar. Anahtarı bilmeyen biri zinciri baştan yazarsa bile doğrulama başarısız olur. "
               "En güvenlisi anahtarı IZIN_AUDIT_KEY ortam değişkeniyle vermek ve ayrıca saklamaktır; "
               "anahtar kaybolursa eski HMAC kayıtları doğrulanamaz. data/.audit_key yedeklere konmaz.")
    if st.button("Şimdi çapa (anchor) al"):
        try:
            a = audit.anchor_now()
            st.success(f"Çapa alındı: log #{a['log_id']}")
        except ValueError as exc:
            st.error(str(exc))
    st.write(f"Toplam çapa: {rep['anchor_count']} · Log: {rep['log_count']}")
    for p in rep["chain"] + rep["consistency"]:
        st.error(p)
    if not rep["chain"] and not rep["consistency"]:
        st.success("Bütünlük kontrolleri temiz.")
