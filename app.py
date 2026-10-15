"""
İzin Takip ve Denetim Sistemi — giriş noktası.
Çalıştırma:  streamlit run app.py
"""
import time
import uuid
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="İzin Takip Sistemi", page_icon="🗂️", layout="wide")

# --- Kurulum doğrulaması -------------------------------------------------------------
# Her dosyanın sonunda sürüm işareti bulunur. Eksik, kesilmiş veya eski sürüm bir dosya varsa
# kriptik hatalar yerine hangi dosyanın sorunlu olduğu söylenir (proje modülleri import edilmeden ÖNCE).
VERSION = "2.3"
_ROOT = Path(__file__).resolve().parent
_EXPECTED = [
    "core/audit.py", "core/backup.py", "core/bootstrap.py", "core/calendar_utils.py", "core/config.py",
    "core/documents.py", "core/notify.py", "core/services.py", "core/storage.py", "core/utils.py",
    "ui/components.py",
    "views/admin.py", "views/audit_view.py", "views/balances.py", "views/dashboard.py", "views/new_request.py",
    "views/reports.py", "views/requests_list.py", "views/team_calendar.py",
]
_bad = []
for _rel in _EXPECTED:
    try:
        _ok = (_ROOT / _rel).read_text(encoding="utf-8").rstrip().endswith(f"# END-OF-FILE v{VERSION}")
    except OSError:
        _ok = False
    if not _ok:
        _bad.append(_rel)
if _bad:
    st.error(f"⚠️ **Kurulum eksik veya karışık (beklenen sürüm {VERSION}).** Şu dosyalar eksik, kesilmiş ya da eski sürümde:")
    st.code("\n".join(_bad), language=None)
    st.markdown("Bu dosyaları (en güvenlisi **tüm klasörleri**) en güncel zip'ten eksiksiz yükleyip uygulamayı yeniden başlatın. "
                "Dosyayı GitHub web arayüzüne yapıştırıyorsanız uzun dosyaların kesilmediğinden emin olun; "
                "dosyanın son satırı `# END-OF-FILE v" + VERSION + "` olmalıdır.")
    st.stop()

from filelock import Timeout  # noqa: E402

from core import backup, bootstrap, config, services, storage  # noqa: E402
from ui import components as ui  # noqa: E402


@st.cache_resource
def _init() -> bool:
    bootstrap.init()
    return True


_init()
if "_yedek_ok" not in st.session_state:
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

# Veri bütünlüğü özeti: oturumda en geç 2 dakikada bir (ya da her başarılı işlemden sonra) yenilenir
try:
    health = st.session_state.get("_saglik")
    if health is None or time.time() - health["t"] > 120:
        health = {"t": time.time(), "rep": services.integrity_report()}
        st.session_state["_saglik"] = health
    rep = health["rep"]
    if rep["chain"] or rep["consistency"]:
        st.error("🚨 **Veri bütünlüğü uyarısı:** kayıtlarda tutarsızlık tespit edildi. Ayrıntı için *Audit Trail* sayfasına bakın; "
                 "gerekirse Yönetim > Yedek'ten önceki bir yedeğe dönebilirsiniz.")
except Exception:  # sağlık kontrolü uygulamayı asla düşürmemeli
    storage.log.exception("Bütünlük kontrolü çalışmadı")

ui.show_flash()
ui.show_bulk_report()
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
