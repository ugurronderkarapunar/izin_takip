"""Ana sayfa: yapılacaklar kuyrukları, uyarılar ve bugünün durumu."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import calendar_utils, config, notify, services
from core.utils import fmt_date
from ui import components as ui

st.title("🏠 Yapılacaklar")
requests = services.load_requests()
settings = services.get_settings()
q = services.queues(requests)
alerts = services.alerts(requests, settings)
today = date.today()
off_today = services.people_on_leave(requests, today)

m = st.columns(5)
m[0].metric("Onay bekleyen", len(q["onay"]))
m[1].metric("Çıktı alınacak", len(q["cikti"]))
m[2].metric("İK'ya teslim edilecek", len(q["teslim"]))
m[3].metric("SAP'ye girilecek", len(q["sap"]))
m[4].metric("Bugün izinli", len(off_today), help=", ".join(off_today) or "Kimse izinli değil")

# --- Uyarılar -----------------------------------------------------------------
if alerts:
    crit = sum(a["seviye"] == "kritik" for a in alerts)
    title = f"⚠️ Uyarılar ({len(alerts)}" + (f" · {crit} kritik)" if crit else ")")
    with st.expander(title, expanded=bool(crit)):
        for i, a in enumerate(alerts[:25]):
            c = st.columns([6, 1, 1, 1])
            (c[0].error if a["seviye"] == "kritik" else c[0].warning)(f"**{a['talep_id']}** · {a['personel']} — {a['mesaj']}")
            if c[1].button("Aç", key=f"al_open_{i}"):
                ui.open_request(a["talep_id"])
            phone_key, mail_key = {"Yönetici": ("yonetici_telefon", "yonetici_eposta"),
                                   "İK": ("ik_telefon", "ik_eposta")}.get(a["kime"], (None, None))
            if phone_key:
                subject, body = notify.alert_message(a)
                wa = notify.whatsapp_link(settings.get(phone_key), body)
                ml = notify.mailto_link(settings.get(mail_key), subject, body)
                if wa:
                    c[2].link_button("WhatsApp", wa)
                if ml:
                    c[3].link_button("E-posta", ml)
        if len(alerts) > 25:
            st.caption(f"… ve {len(alerts) - 25} uyarı daha.")
else:
    st.success("Gecikmiş iş yok. 🎉")


# --- Kuyruklar ----------------------------------------------------------------
def queue_list(items: list[dict], kind: str) -> None:
    if not items:
        st.success("Bu kuyrukta bekleyen iş yok.")
        return
    for r in items[:15]:
        c = st.columns([5, 1, 2])
        c[0].markdown(f"**{r['id']}** · {r['personel_adi']} · {fmt_date(r['baslangic'])} – {fmt_date(r['bitis'])} · {r['is_gunu']} iş günü")
        if c[1].button("Aç", key=f"q_{kind}_open_{r['id']}"):
            ui.open_request(r["id"])
        if kind == "onay" and ui.can("onay"):
            a, b = c[2].columns(2)
            if a.button("✅", key=f"q_{kind}_ok_{r['id']}", help="Onayla"):
                ui.dialog_status(r["id"], config.S_APPROVED)
            if b.button("❌", key=f"q_{kind}_no_{r['id']}", help="Reddet"):
                ui.dialog_status(r["id"], config.S_REJECTED)
        elif kind == "cikti" and ui.can("cikti"):
            if c[2].button("🖨️ Çıktı alındı", key=f"q_{kind}_do_{r['id']}"):
                if ui.run(services.mark_printed, r["id"], ok_msg="Çıktı alındı olarak işaretlendi"):
                    st.rerun()
        elif kind == "teslim" and ui.can("teslim"):
            if c[2].button("📨 Teslim et", key=f"q_{kind}_do_{r['id']}"):
                ui.dialog_deliver(r["id"])
        elif kind == "sap" and ui.can("sap"):
            if c[2].button("🧾 SAP'ye girildi", key=f"q_{kind}_do_{r['id']}"):
                ui.dialog_sap(r["id"])
    if len(items) > 15:
        st.caption(f"… ve {len(items) - 15} kayıt daha: 'Talepler' sayfasındaki filtreleri kullanın.")


tabs = st.tabs([f"Onay bekleyen ({len(q['onay'])})", f"Çıktı alınacak ({len(q['cikti'])})",
                f"Teslim edilecek ({len(q['teslim'])})", f"SAP'ye girilecek ({len(q['sap'])})"])
for tab, kind in zip(tabs, ["onay", "cikti", "teslim", "sap"]):
    with tab:
        queue_list(q[kind], kind)

# --- Önümüzdeki günler --------------------------------------------------------
st.subheader("📅 Önümüzdeki 14 gün")
end = today + timedelta(days=13)
people = calendar_utils.daily_people(requests, today, end, config.ACTIVE_STATUSES)
hol = calendar_utils.holiday_set()
rows = [{"Tarih": d, "Gün": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"][d.weekday()],
         "İzinli": len(s), "Kişiler": ", ".join(sorted(s)),
         "Not": "Resmî tatil" if d in hol else ""} for d, s in sorted(people.items()) if s or d in hol]
if rows:
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Tarih": st.column_config.DateColumn(format="DD.MM.YYYY")})
else:
    st.caption("Önümüzdeki 14 günde izinli kişi yok.")
