"""Yeni izin talebi: canlı önizleme (iş günü, bakiye, ekip yoğunluğu) ile giriş."""
from datetime import date

import streamlit as st

from core import config, services
from ui import components as ui

st.title("➕ Yeni İzin Talebi")
if not ui.can("talep_gir"):
    st.info("Bu rolün talep girişi yetkisi yok.")
    st.stop()

nonce = st.session_state.setdefault("yt_nonce", 0)
k = lambda n: f"yt_{nonce}_{n}"  # noqa: E731  (kayıt sonrası anahtarlar yenilenir; hata durumunda girilenler korunur)

NEW = "➕ Listede yok — yeni personel ekle"
people = [p["ad"] for p in services.list_personnel(active_only=True)]

c1, c2 = st.columns(2)
sel = c1.selectbox("Personel *", people + [NEW], index=None, placeholder="Seçin…", key=k("personel"))
ad = c1.text_input("Yeni personel adı soyadı *", key=k("yeni_ad")) if sel == NEW else (sel or "")
tur = c2.selectbox("İzin türü *", config.LEAVE_TYPES, key=k("tur"))
d1, d2 = st.columns(2)
start = d1.date_input("Başlangıç *", value=date.today(), format="DD.MM.YYYY", key=k("bas"))
end = d2.date_input("Bitiş *", value=date.today(), format="DD.MM.YYYY", key=k("bit"))
aciklama = st.text_area("Açıklama", key=k("acik"), placeholder="İsteğe bağlı not")
files = st.file_uploader("Ek dosyalar (rapor, dilekçe vb.)", accept_multiple_files=True, key=k("ek"),
                         type=[e.strip(".") for e in sorted(config.ALLOWED_EXT)])

# --- Canlı önizleme -----------------------------------------------------------
pv = services.preview_request(ad, tur, start, end)
m = st.columns(4)
m[0].metric("İş günü", pv["is_gunu"])
m[1].metric("Takvim günü", pv["takvim_gun"])
b = pv["bakiye"]
if b:
    m[2].metric("Kullanılabilir bakiye", f"{b['kullanilabilir']:g} gün", delta=f"{-pv['is_gunu']} gün", delta_color="off")
    m[3].metric("Talepten sonra kalan", f"{b['kullanilabilir'] - pv['is_gunu']:g} gün")
for e in pv["errors"]:
    st.error(e)
for w in pv["warnings"]:
    st.warning(w)
override = False
if pv["bakiye_yetersiz"]:
    st.warning("Yıllık izin bakiyesi yetersiz.")
    override = st.checkbox("Bakiye yetersiz olsa da kaydet (loga işlenir)", key=k("override"))

blocked = bool(pv["errors"]) or not ad.strip() or (pv["bakiye_yetersiz"] and not override)
if st.button("Talebi kaydet", type="primary", disabled=blocked):
    if ui.user_ok():
        try:
            tid = services.create_request(ui.current_user(), ui.current_role(), ad, tur, start, end, aciklama,
                                          [(f.name, f.getvalue()) for f in files or []], bakiye_override=override)
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))  # form içeriği silinmez
        else:
            ui.flash(f"Talep oluşturuldu: {tid} ({config.S_PENDING})")
            st.session_state["yt_nonce"] = nonce + 1
            st.rerun()
