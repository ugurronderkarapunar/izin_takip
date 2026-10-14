"""
İzin Takip ve Denetim Sistemi — giriş noktası.
Çalıştırma:  streamlit run app.py
"""
import time
import uuid

import streamlit as st
from filelock import Timeout

from core import backup, bootstrap, config, storage

st.set_page_config(page_title="İzin Takip Sistemi", page_icon="🗂️", layout="wide")


@st.cache_resource
def _init() -> bool:
    bootstrap.init()
    return True


EXPECTED_VERSION = "2.2"
from ui import components as ui  # noqa: E402

if config.APP_VERSION != EXPECTED_VERSION or getattr(ui, "UI_VERSION", None) != EXPECTED_VERSION:
    st.error("⚠️ **Dosya sürümleri uyuşmuyor.** `app.py`, `core/`, `ui/` ve `views/` klasörleri farklı sürümlerden karışmış. "
             "Tüm klasörleri (özellikle `core/` ve `ui/`) en güncel zip'ten eksiksiz yükleyip uygulamayı yeniden başlatın.")
    st.stop()

_init()

# Günlük yedekleme (services modülü güvenli biçimde burada çağrılır)
if "_yedek_ok" not in st.session_state:
    from core import services
    backup.daily_backup(int(services.get_settings()["yedek_saklama_gun"]))
    st.session_state["_yedek_ok"] = True

with st.sidebar:
    st.text_input("Kullanıcı adı *", key="kullanici", placeholder="Örn: Uğur")
    st.selectbox("Rol", config.ROLES, index=config.ROLES.index("Planlama"), key="rol",
                 help="Rol, hangi işlemlerin görüneceğini belirler. Kimlik doğrulama değildir; yapılan her işlem kullanıcı adı ve rolle loglanır.")

role = st.session_state.get("rol", "Planlama")
sections: dict[str, list[st.Page]] = {
    "İş": [st.Page("views/dashboard.py", title="Yapılacaklar", icon="🏠", default=True)]
         + ([st.Page("views/new_request.py", title="Yeni Talep", icon="➕")] if config.can(role, "talep_gir") else [])
         + [st.Page("views/requests_list.py", title="Talepler", icon="📋")],
    "Planlama": [st.Page("views/team_calendar.py", title="Ekip Takvimi", icon="📅"),
                 st.Page("views/balances.py", title="Bakiyeler", icon="🧮")],
    "Analiz ve Denetim": ([st.Page("views/reports.py", title="Raporlar", icon="📊")] if role != "Personel" else [])
                         + [st.Page("views/audit_view.py", title="Audit Trail", icon="🛡️")],
}
if config.can(role, "admin"):
    sections["Sistem"] = [st.Page("views/admin.py", title="Yönetim", icon="⚙️")]

nav = st.navigation(sections)
for w in storage.pop_warnings():
    st.warning(w)

# Veri bütünlüğü özeti (services yalnızca ihtiyaç anında yüklenir)
try:
    health = st.session_state.get("_saglik")
    if health is None or time.time() - health["t"] > 120:
        from core import services
        health = {"t": time.time(), "rep": services.integrity_report()}
        st.session_state["_saglik"] = health
    rep = health["rep"]
    if rep["chain"] or rep["consistency"]:
        st.error("🚨 **Veri bütünlüğü uyarısı:** kayıtlarda tutarsızlık tespit edildi. Ayrıntı için *Audit Trail* sayfasına bakın; "
                 "gerekirse Yönetim > Yedek'ten önceki bir yedeğe dönebilirsiniz.")
except Exception:  # sağlık kontrolü uygulamayı asla düşürmemeli
    storage.log.exception("Bütünlük kontrolü çalışmadı")

ui.show_flash()
try:
    nav.run()
except Timeout:
    st.error("Veri dosyası şu an başka bir işlem tarafından kullanılıyor. Birkaç saniye sonra tekrar deneyin.")
except Exception as exc:  # Streamlit'in rerun/stop istisnaları BaseException'dır, buraya düşmez
    err_id = uuid.uuid4().hex[:8]
    storage.log.exception("Beklenmeyen hata [%s]", err_id)
    st.error(f"Beklenmeyen bir hata oluştu (hata kodu: **{err_id}**). Kayıtlarınız korunmuştur; ayrıntı `data/app.log` dosyasındadır.")
    if role == "Admin":
        st.exception(exc)
    if st.button("Sayfayı yenile"):
        st.rerun()
