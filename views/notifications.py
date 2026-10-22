"""Bildirim merkezi: başkalarının işlemleri, gecikme uyarıları ve geri alınabilir işlemler."""
import streamlit as st

from core import inbox, notify, services
from core.utils import fmt_dt
from ui import components as ui

st.title("🔔 Bildirim Merkezi")
user = ui.current_user()
if not user:
    st.info("Bildirimleri görmek için sol menüden kullanıcınızı seçin.")
    st.stop()

requests = services.load_requests(include_deleted=True)
names = {r["id"]: r["personel_adi"] for r in requests}
unread = inbox.unread(user)
alerts = services.alerts(services.load_requests(), services.get_settings())
settings = services.get_settings()

t_new, t_alert, t_feed = st.tabs([f"Yeni ({len(unread)})", f"Gecikme uyarıları ({len(alerts)})", "Son işlemler"])

with t_new:
    if not unread:
        st.success("Okunmamış bildirim yok.")
    else:
        if st.button("✔️ Hepsini okundu işaretle", type="primary", key="inbox_read"):
            inbox.mark_read(user)
            ui.flash("Bildirimler okundu işaretlendi")
            st.rerun()
        for e in reversed(unread[-50:]):
            c = st.columns([7, 2, 1])
            c[0].markdown(inbox.describe(e, names))
            c[1].caption(fmt_dt(e["zaman"]))
            if e["talep_id"] in names and c[2].button("Aç", key=f"in_open_{e['log_id']}"):
                ui.open_request(e["talep_id"])

with t_alert:
    if not alerts:
        st.success("Gecikmiş iş yok. 🎉")
    for i, a in enumerate(alerts[:40]):
        c = st.columns([6, 1, 1, 1])
        (c[0].error if a["seviye"] == "kritik" else c[0].warning)(f"**{a['talep_id']}** · {a['personel']} — {a['mesaj']}")
        if c[1].button("Aç", key=f"al_open_{i}"):
            ui.open_request(a["talep_id"])
        keys = {"Yönetici": ("yonetici_telefon", "yonetici_eposta"), "İK": ("ik_telefon", "ik_eposta")}.get(a["kime"])
        if keys:
            subject, body = notify.alert_message(a)
            wa, ml = notify.whatsapp_link(settings.get(keys[0]), body), notify.mailto_link(settings.get(keys[1]), subject, body)
            if wa:
                c[2].link_button("WhatsApp", wa)
            if ml:
                c[3].link_button("E-posta", ml)

with t_feed:
    feed = inbox.feed(user, 40)
    if not feed:
        st.caption("Başkalarının yaptığı işlem yok.")
    for e in feed:
        st.markdown(f"{inbox.describe(e, names)}  \n<small>{fmt_dt(e['zaman'])}</small>", unsafe_allow_html=True)
    st.divider()
    st.markdown("**↩️ Geri alınabilir işlemler**")
    items = services.recent_undoable(10)
    if not items:
        st.caption("Şu an geri alınabilir işlem yok.")
    for e in items:
        c = st.columns([7, 2])
        c[0].markdown(f"**{e['islem']}** · {e['talep_id']} · {names.get(e['talep_id'], '')} — {e['kullanici']}, {fmt_dt(e['zaman'])}")
        if c[1].button("Geri al", key=f"nc_undo_{e['log_id']}"):
            if ui.run(services.undo_last, e["talep_id"], ok_msg=f"{e['talep_id']}: {e['islem']} geri alındı"):
                ui.reset_table_selection()
                st.rerun()

# END-OF-FILE v3.2
