"""Yönetim: personel ve izin hakkı, ayarlar, tatiller, yedek, güvenlik, sistem durumu."""
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from core import audit, backup, calendar_utils, config, notifier, services, storage
from ui import components as ui

st.title("⚙️ Yönetim")
if not ui.can("admin"):
    st.warning("Bu sayfa için yetkiniz yok.")
    st.stop()

t_per, t_set, t_hol, t_bak, t_sec, t_sys = st.tabs(["👥 Personel", "🎛️ Ayarlar", "🎌 Tatiller", "💾 Yedek", "🔐 Güvenlik", "🩺 Sistem durumu"])

# ------------------------------------------------------------------ Personel
with t_per:
    people = services.list_personnel()
    yil = st.selectbox("Yıl", list(range(date.today().year - 1, date.today().year + 3)), index=1, key="pe_year",
                       help="Tablo ve izin hakkı düzenleme bu yıl için geçerlidir.")
    if people:
        st.dataframe(services.balance_table(yil), hide_index=True, width="stretch",
                     column_config={"Aktif": st.column_config.CheckboxColumn("Aktif")})
        st.markdown("**Personeli düzenle**")
        if "_pe_next" in st.session_state:  # yeniden adlandırma sonrası seçimi yeni ada taşı
            st.session_state["pe_person"] = st.session_state.pop("_pe_next")
        sel = st.selectbox("Personel", [p["ad"] for p in people], key="pe_person")
        p = services.get_person(sel)
        b = services.balance_for(sel, yil)
        _, kaynak = services.entitlement(p, yil)
        kb = lambda n: f"pe_{sel}_{yil}_{n}"  # noqa: E731  (kişi/yıl değişince alanlar yeniden doldurulur)
        with st.form("pe_form"):
            birim_adlari = [b["ad"] for b in services.get_settings()["birimler"]]
            c1, c2, c3, c4 = st.columns(4)
            yeni_ad = c1.text_input("Ad soyad", sel, key=kb("ad"))
            birim = c2.selectbox("Birim", birim_adlari, index=birim_adlari.index(p["birim"]) if p["birim"] in birim_adlari else 0, key=kb("birim"),
                                 help="Gemi birimlerinde hafta sonu çalışılır ve izin gününden sayılır. Birim değişince mevcut taleplerin iş günü yeniden hesaplanır.")
            ekip = c3.text_input("Ekip", p.get("ekip", ""), key=kb("ekip"))
            tel = c4.text_input("Telefon", p.get("telefon", ""), key=kb("tel"))
            g1, g2, g3, g4 = st.columns(4)
            giris = g1.date_input("İşe giriş tarihi", value=date.fromisoformat(p["ise_giris"]) if p.get("ise_giris") else None,
                                  min_value=date(1960, 1, 1), max_value=date.today(), format="DD.MM.YYYY", key=kb("giris"),
                                  help="Girilirse yıllık izin hakkı kıdeme göre otomatik hesaplanır (Ayarlar > İzin hakkı).")
            hak = g2.number_input(f"{yil} yıllık izin hakkı (gün)", 0.0, 366.0, float(b["hak"]), step=0.5, format="%.1f", key=kb("hak"))
            dev = g3.number_input(f"{yil} devreden (gün)", 0.0, 366.0, float(b["devreden"]), step=0.5, format="%.1f", key=kb("dev"))
            aktif = g4.checkbox("Aktif personel", p.get("aktif", True), key=kb("aktif"))
            gerekce = st.text_input("Not / gerekçe (isteğe bağlı)", key=kb("not"))
            kaydet = st.form_submit_button("💾 Kaydet", type="primary")
        st.caption(f"{yil} için şu an: hak {b['hak']:g} ({kaynak}) · devreden {b['devreden']:g} · kullanılan {b['kullanilan']:g} · "
                   f"kalan {b['kalan']:g}. Hak ve devreden değişikliği **yalnızca {yil} yılını** etkiler. Personel silinmez; ayrılanlar pasife alınır.")
        if str(yil) in p["hak_yillik"] or str(yil) in p["devreden_yillik"]:
            if st.button(f"↺ {yil} için elle girilen değeri kaldır (otomatik hesaba dön)", key=f"pe_clear_{sel}_{yil}"):
                if ui.run(services.clear_year_entitlement, sel, yil, ok_msg=f"{sel}: {yil} değeri otomatik hesaba döndü"):
                    st.rerun()
        if kaydet:
            msgs: list[str] = []
            ok = True
            if (hak, dev) != (float(b["hak"]), float(b["devreden"])):
                ok = ui.run(services.set_year_entitlement, sel, yil, hak, dev, gerekce, ok_msg="")
                if ok:
                    msgs.append(f"{yil} hakkı {b['hak']:g} → {hak:g}, devreden {b['devreden']:g} → {dev:g}")
            profile = {}
            if birim != p["birim"]:
                profile["birim"] = birim
            if ekip.strip() != p.get("ekip", ""):
                profile["ekip"] = ekip.strip()
            if tel.strip() != p.get("telefon", ""):
                profile["telefon"] = tel.strip()
            if aktif != p.get("aktif", True):
                profile["aktif"] = aktif
            if (giris.isoformat() if giris else None) != p.get("ise_giris"):
                profile["ise_giris"] = giris.isoformat() if giris else None
            if ok and profile:
                ok = ui.run(services.update_personnel, sel, not_=gerekce, ok_msg="", **profile)
                if ok:
                    msgs.append("bilgiler güncellendi (" + ", ".join(profile) + ")")
            if ok and yeni_ad.strip() != sel:
                ok = ui.run(services.rename_personnel, sel, yeni_ad.strip(), ok_msg="")
                if ok:
                    msgs.append(f"ad: {sel} → {yeni_ad.strip()}")
                    st.session_state["_pe_next"] = yeni_ad.strip()
            if ok and msgs:
                ui.flash(f"{sel}: " + " · ".join(msgs))
                st.rerun()
            elif ok:
                st.info("Değişiklik yok.")
    if people:
        with st.expander("🚢 Toplu birim ata"):
            st.caption("Birimi henüz atanmamış (varsayılan: Ofis) birden fazla personeli tek seferde doğru birime alın.")
            tb_names = st.multiselect("Personel", [p_["ad"] for p_ in people], key="tb_names")
            tb_birim = st.selectbox("Birim", [b["ad"] for b in services.get_settings()["birimler"]], key="tb_birim")
            if st.button("Birimi ata", disabled=not tb_names, key="tb_go"):
                if ui.run(services.bulk_set_birim, tb_names, tb_birim, ok_msg=f"{len(tb_names)} personelin birimi '{tb_birim}' olarak güncellendi"):
                    st.rerun()
    with st.form("pers_add", clear_on_submit=False):
        st.markdown("**Yeni personel ekle**")
        a1, a2, a3, a4, a5, a6 = st.columns(6)
        ad = a1.text_input("Ad soyad *")
        yb = a2.selectbox("Birim", [b["ad"] for b in services.get_settings()["birimler"]])
        ekip = a3.text_input("Ekip")
        tel = a4.text_input("Telefon")
        hak = a5.number_input("Yıllık hak", 0, 60, int(services.get_settings()["yillik_hak_varsayilan"]))
        dev = a6.number_input(f"{date.today().year} devreden", 0, 60, 0)
        if st.form_submit_button("Ekle") and ui.run(services.add_personnel, ad, hak, dev, tel, ekip, yb, ok_msg=f"{ad} eklendi"):
            st.rerun()

# ------------------------------------------------------------------ Ayarlar
with t_set:
    s = services.get_settings()
    with st.form("ayarlar"):
        st.markdown("**Kullanıcılar** (veri girişi yapacak kişiler; yapılan her işlem bu adla loglanır)")
        users_txt = st.text_area("Her satıra bir ad", "\n".join(s["kullanicilar"]), height=90,
                                 help="Doluysa giriş ekranında bu listeden seçilir; boşsa ad serbest yazılır.")
        st.markdown("**Birimler ve çalışma düzeni** — hafta sonu çalışılan birimlerde (ör. gemi personeli) hafta sonu günleri izin gününden sayılır. "
                    "Kendi uygulamanıza / sözleşmenize göre düzenleyin. Personelde kullanılan bir birim silinemez; adı boş bırakılan satır kullanılmaz.")
        bs = list(s["birimler"]) + [{"ad": "", "hafta_sonu": False, "resmi_tatil": False}] * 3
        birimler_yeni = []
        for i in range(len(s["birimler"]) + 3):
            b1, b2, b3 = st.columns([3, 1, 1])
            bad = b1.text_input(f"{i + 1}. birim adı", bs[i]["ad"], key=f"bi_ad{i}")
            bhs = b2.checkbox("Hafta sonu çalışılır", bs[i]["hafta_sonu"], key=f"bi_hs{i}")
            brt = b3.checkbox("Resmî tatil çalışılır", bs[i]["resmi_tatil"], key=f"bi_rt{i}", help="İşaretliyse resmî tatil / arife günleri de izin gününden sayılır.")
            if bad.strip():
                birimler_yeni.append({"ad": bad.strip(), "hafta_sonu": bhs, "resmi_tatil": brt})
        st.markdown("**Süreç eşikleri**")
        a1, a2, a3, a4 = st.columns(4)
        hak_v = a1.number_input("Varsayılan yıllık hak", 0.0, 366.0, float(s["yillik_hak_varsayilan"]), step=1.0)
        esik = a2.number_input("Aynı gün izinli eşiği", 1, 100, int(s["esik_ayni_anda"]))
        gsaat = a3.number_input("Günlük çalışma saati", 1.0, 24.0, float(s["gunluk_calisma_saati"]), step=0.5,
                                help="Saatlik izni güne çevirmek için (ör. 2 saat = 2/8 gün).")
        yedek = a4.number_input("Yedek saklama (gün)", 1, 3650, int(s["yedek_saklama_gun"]))
        b1, b2, b3, b4 = st.columns(4)
        u1 = b1.number_input("Onay bekleme uyarısı (gün)", 0, 60, int(s["uyari_bekleyen_gun"]))
        u2 = b2.number_input("Çıktı alınmama uyarısı (gün)", 0, 60, int(s["uyari_cikti_gun"]))
        u3 = b3.number_input("Teslim edilmeme uyarısı (gün)", 0, 60, int(s["uyari_teslim_gun"]))
        u4 = b4.number_input("SAP girişi uyarısı (gün)", 0, 60, int(s["uyari_sap_gun"]))
        st.markdown("**İzin hakkı (kıdem)** — varsayılan değerler 4857 sayılı İş Kanunu m.53 asgari süreleridir; "
                    "kurumunuzun toplu sözleşmesi farklıysa değiştirin.")
        otomatik = st.toggle("İşe giriş tarihi girilmiş personelin hakkını kıdemden otomatik hesapla", value=bool(s["otomatik_hak"]))
        kd = list(s["kidem_tablosu"]) + [{"min_yil": 0, "hak": 0}] * 3
        k_cols = st.columns(3)
        kidem = []
        for i, col in enumerate(k_cols):
            my = col.number_input(f"{i + 1}. kademe: en az (yıl)", 0, 60, int(kd[i]["min_yil"]), key=f"kd_y{i}")
            kh = col.number_input(f"{i + 1}. kademe: hak (gün)", 0.0, 366.0, float(kd[i]["hak"]), step=1.0, key=f"kd_h{i}")
            kidem.append({"min_yil": my, "hak": kh})
        st.markdown("**Bildirim kişileri** (uyarılardaki WhatsApp / e-posta bağlantıları ve otomatik e-posta için)")
        c1, c2, c3, c4 = st.columns(4)
        yt = c1.text_input("Yönetici telefon", s["yonetici_telefon"])
        ye = c2.text_input("Yönetici e-posta", s["yonetici_eposta"])
        it = c3.text_input("İK telefon", s["ik_telefon"])
        ie = c4.text_input("İK e-posta", s["ik_eposta"])
        d1, d2 = st.columns(2)
        pe = d1.text_input("Planlama e-posta", s["planlama_eposta"])
        oz = d2.text_input("Özet alıcıları (virgülle ayırın)", s["ozet_alicilari"], help="Tüm uyarıların ve sistem sorunlarının özetini alır.")
        st.markdown("**Otomatik e-posta bildirimi ve belge doğrulama**")
        e1, e2, e3 = st.columns([1, 1, 3])
        b_aktif = e1.toggle("Günlük e-posta özeti", value=bool(s["bildirim_aktif"]))
        b_saat = e2.text_input("Saat (SS:DD)", s["bildirim_saat"])
        url = e3.text_input("Uygulama adresi", s["uygulama_url"], placeholder="https://izin-takip.streamlit.app",
                            help="İzin kağıdındaki QR kod bu adrese götürür. Boşsa QR kod basılmaz.")
        if st.form_submit_button("Ayarları kaydet", type="primary"):
            new = {"kullanicilar": [u.strip() for u in users_txt.splitlines() if u.strip()], "yillik_hak_varsayilan": hak_v,
                   "esik_ayni_anda": esik, "gunluk_calisma_saati": gsaat, "yedek_saklama_gun": yedek,
                   "uyari_bekleyen_gun": u1, "uyari_cikti_gun": u2, "uyari_teslim_gun": u3, "uyari_sap_gun": u4,
                   "otomatik_hak": otomatik, "kidem_tablosu": kidem,
                   "yonetici_telefon": yt, "yonetici_eposta": ye, "ik_telefon": it, "ik_eposta": ie,
                   "planlama_eposta": pe, "ozet_alicilari": oz, "bildirim_aktif": b_aktif, "bildirim_saat": b_saat,
                   "uygulama_url": url.strip(), "birimler": birimler_yeni}
            if ui.run(services.save_settings, new, ok_msg="Ayarlar kaydedildi"):
                st.rerun()

# ------------------------------------------------------------------ Tatiller
with t_hol:
    st.caption("Dinî bayram tarihleri her yıl değişir; ilgili yılın Diyanet takvimiyle kontrol edip güncelleyin. "
               "Arife günleri **yarım gün** olarak işaretlenir (izin hesabında 0,5 iş günü).")
    hol = calendar_utils.load_holidays()
    st.dataframe(pd.DataFrame(hol).rename(columns={"tarih": "Tarih", "ad": "Tatil", "yarim": "Yarım gün"}), hide_index=True, width="stretch", height=260)
    with st.form("hol_add"):
        h1, h2, h3 = st.columns([2, 3, 1])
        hd = h1.date_input("Tarih", value=date.today(), format="DD.MM.YYYY")
        hn = h2.text_input("Tatil adı")
        hy = h3.checkbox("Yarım gün")
        if st.form_submit_button("Tatil ekle") and ui.run(services.add_holiday, hd, hn, hy, ok_msg="Tatil eklendi"):
            st.rerun()
    rm = st.selectbox("Tatil kaldır", [h["tarih"] + " · " + h["ad"] for h in hol], index=None, placeholder="Seçin…")
    if rm and st.button("Seçili tatili kaldır"):
        if ui.run(services.remove_holiday, rm.split(" · ")[0], ok_msg="Tatil kaldırıldı"):
            st.rerun()

# ------------------------------------------------------------------ Yedek
with t_bak:
    be = storage.get_backend()
    st.caption("Her gün ilk açılışta otomatik yedek alınır. " + ("Yedekler veritabanında saklanır (uygulama yeniden başlasa da kaybolmaz)."
               if be.kind == "veritabanı" else "Yedekler data/backups klasöründedir."))
    b1, b2 = st.columns(2)
    if b1.button("Şimdi yedek al", width="stretch"):
        st.success(f"Yedek alındı: {backup.backup_now('manuel')}")
    b2.download_button("⬇️ Tüm veriyi ZIP indir (ekler dahil)", backup.zip_bytes(), f"izin_takip_yedek_{date.today().isoformat()}.zip",
                       "application/zip", width="stretch")
    st.write(f"Mevcut yedekler ({len(backup.list_backups())}):")
    st.code("\n".join(backup.list_backups()[:30]) or "—", language=None)
    st.divider()
    st.markdown("**Yedekten geri yükle**")
    st.warning("Geri yükleme, seçilen yedekten sonra yapılan tüm kayıtları geri alır. İşlemden önce otomatik güvenlik yedeği alınır "
               "ve olay loga yazılır. Audit sayfasındaki mühür, geri alınan kayıtları ortaya çıkarır.")
    pick = st.selectbox("Yedek", backup.list_backups(), index=None, placeholder="Seçin…", key="rest_pick")
    conf = st.text_input("Onaylamak için GERİ YÜKLE yazın", key="rest_conf")
    if st.button("Yedeğe dön", key="rest_go", disabled=not (pick and conf.strip() == "GERİ YÜKLE")):
        if ui.run(services.restore_backup, pick, ok_msg=f"{pick} yedeğine dönüldü"):
            st.rerun()

# ------------------------------------------------------------------ Güvenlik
with t_sec:
    rep = services.integrity_report()
    st.write(f"Doğrulama anahtarı kaynağı: **{rep['key_source']}**")
    st.caption("Anahtar, log zincirini HMAC ile imzalar ve izin formundaki QR doğrulamasında kullanılır. Anahtarı bilmeyen biri zinciri baştan "
               "yazsa bile doğrulama başarısız olur. En güvenlisi anahtarı Streamlit secrets'ta `audit_key` olarak vermek ve ayrıca saklamaktır; "
               "anahtar kaybolursa eski kayıtlar doğrulanamaz.")
    if st.button("Şimdi çapa (anchor) al"):
        try:
            a = audit.anchor_now()
            st.success(f"Çapa alındı: log #{a['log_id']}")
        except ValueError as exc:
            st.error(str(exc))
    st.write(f"Toplam çapa: {rep['anchor_count']} · Log: {rep['log_count']}")
    for p_ in rep["chain"] + rep["consistency"]:
        st.error(p_)
    if not rep["chain"] and not rep["consistency"]:
        st.success("Bütünlük kontrolleri temiz.")

# ------------------------------------------------------------------ Sistem durumu (gözlem)
with t_sys:
    be = storage.get_backend()
    try:
        ping, ping_ok = be.ping(), True
    except Exception as exc:  # bağlantı sorunu burada görünür olmalı
        ping, ping_ok = f"HATA: {exc}", False
    rep = services.integrity_report()
    reqs = services.load_requests(include_deleted=True)
    m = st.columns(5)
    m[0].metric("Depolama", be.kind)
    m[1].metric("Bağlantı", "Tamam" if ping_ok else "SORUN")
    m[2].metric("Sürüm", config.APP_VERSION)
    m[3].metric("Talep (silinen)", f"{len([r for r in reqs if not r['silindi']])} ({len([r for r in reqs if r['silindi']])})")
    m[4].metric("Log / çapa", f"{rep['log_count']} / {rep['anchor_count']}")
    st.caption(f"{be.info()} · {ping}")
    if be.kind == "dosya":
        st.warning("Dosya depolaması kullanılıyor. Streamlit Community Cloud gibi geçici disklerde yeniden başlatmada veri kaybolur; "
                   "kalıcılık için veritabanı bağlantısı (secrets: database_url) tanımlayın.")
    hist = notifier.hist_store.read()
    cfg_ok = notifier.smtp_config() is not None
    n1, n2, n3 = st.columns(3)
    n1.metric("SMTP ayarı", "Var" if cfg_ok else "Yok")
    n2.metric("Günlük e-posta", "Açık" if services.get_settings()["bildirim_aktif"] else "Kapalı")
    n3.metric("Son başarılı gönderim", hist.get("son_basarili", "—"))
    to = st.text_input("Test e-postası adresi", key="sys_test_to")
    t1, t2, t3 = st.columns(3)
    if t1.button("Test e-postası gönder", disabled=not to.strip(), width="stretch"):
        res = notifier.send_test(to.strip())
        (st.success if res["durum"] == "gönderildi" else st.error)(f"Test e-postası: {res['durum']} {res.get('hata', '')}")
    if t2.button("Özeti önizle", width="stretch"):
        res = notifier.run_daily(force=True, dry_run=True)
        st.write(f"{res.get('uyari_sayisi', 0)} uyarı, {len(res.get('mesajlar', []))} e-posta üretilecek.")
        for mm in res.get("mesajlar", []):
            with st.expander(f"{mm['to']} — {mm['subject']}"):
                st.text(mm["text"])
    if t3.button("Özeti şimdi gönder", width="stretch"):
        res = notifier.run_daily(force=True)
        (st.success if res["durum"] == "gönderildi" else st.warning)(f"Sonuç: {res['durum']} {res.get('hata', '')}")
    st.markdown("**Son 30 günde işlem sayısı**")
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    per_day = pd.Series([e["zaman"][:10] for e in audit.logs_store.read() if e["zaman"][:10] >= cutoff]).value_counts().sort_index()
    if len(per_day):
        st.bar_chart(per_day)
    else:
        st.caption("Son 30 günde işlem yok.")
    st.markdown("**Son olaylar (hata / uyarı)**")
    ev = storage.recent_events(50)
    if ev:
        st.dataframe(pd.DataFrame(ev), hide_index=True, width="stretch")
    else:
        st.caption("Kayıtlı olay yok.")

# END-OF-FILE v3.1
