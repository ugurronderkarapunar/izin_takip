"""
İzin Takip ve Denetim Sistemi — giriş noktası.
Çalıştırma:  streamlit run app.py
"""
import os
import time
import uuid
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="İzin Takip Sistemi", page_icon="🗂️", layout="wide")

# --- Kurulum doğrulaması -------------------------------------------------------------
# Her dosyanın sonunda sürüm işareti bulunur. Eksik, kesilmiş veya eski sürüm bir dosya varsa
# kriptik hatalar yerine hangi dosyanın sorunlu olduğu söylenir (proje modülleri import edilmeden ÖNCE).
VERSION = "3.1"
_ROOT = Path(__file__).resolve().parent
_EXPECTED = [
    "core/audit.py", "core/backup.py", "core/bootstrap.py", "core/calendar_utils.py", "core/config.py", "core/documents.py",
    "core/inbox.py", "core/models.py", "core/notifier.py", "core/notify.py", "core/rules.py", "core/services.py",
    "core/storage.py", "core/utils.py", "core/verify.py",
    "ui/calendar_grid.py", "ui/components.py",
    "views/admin.py", "views/audit_view.py", "views/balances.py", "views/dashboard.py", "views/new_request.py",
    "views/notifications.py", "views/reports.py", "views/requests_list.py", "views/team_calendar.py",
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

# --- Streamlit secrets -> ortam değişkenleri (database_url, audit_key, app_password, smtp_*, app_url ...) ---------
try:
    for _k in list(st.secrets.keys()):
        _v = st.secrets[_k]
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(f"IZIN_{str(_k).upper()}", str(_v))
except Exception:  # secrets dosyası yoksa sorun değil (yerel çalışma)
    pass

from filelock import Timeout  # noqa: E402

from core import backup, bootstrap, config, inbox, notifier, services, storage  # noqa: E402
from ui import components as ui  # noqa: E402


@st.cache_resource(show_spinner="Veritabanına bağlanılıyor…")
def _init() -> str:
    bootstrap.init()
    return storage.get_backend().kind


# Veritabanına ulaşılamazsa kullanıcıya anlaşılır bir mesaj göster
try:
    _init()
except Exception as _exc:
    storage.log.exception("Başlatma hatası")
    st.error("🔌 **Veri deposuna bağlanılamadı.** Kayıtlarınız korunmuştur; birkaç dakika sonra tekrar deneyin. "
             "Sorun sürerse veritabanı bağlantı adresini (secrets: `database_url`) kontrol edin.")
    st.exception(_exc) if os.environ.get("IZIN_DEBUG") else None
    st.stop()

# --- İzin kağıdındaki QR kodun açtığı herkese açık doğrulama sayfası (parola kapısından ÖNCE) -------------
if "dogrula" in st.query_params:
    ui.render_verification(str(st.query_params.get("dogrula", "")), str(st.query_params.get("k", "")))
    st.stop()

ui.password_gate()

if "_yedek_ok" not in st.session_state:
    try:
        backup.daily_backup(int(services.get_settings()["yedek_saklama_gun"]))
    except Exception as _e:  # yedek hatası uygulamayı durdurmamalı
        storage.log_event("hata", "yedek", _e)
    st.session_state["_yedek_ok"] = True
if "_bildirim_ok" not in st.session_state:
    notifier.opportunistic()  # ayarlanan saatten sonra günde bir kez e-posta özeti (kapalıysa hemen döner)
    st.session_state["_bildirim_ok"] = True

# --- Yan panel: kullanıcı, bildirim sayacı, geri al ---------------------------------------
st.session_state["rol"] = "Kullanıcı"  # tek rütbe: herkes her işlemi yapabilir
users = services.get_settings()["kullanicilar"]
with st.sidebar:
    if users:
        if st.session_state.get("kullanici") not in users:
            st.session_state.pop("kullanici", None)
        st.selectbox("Kullanıcı *", users, index=None, placeholder="Kim olduğunuzu seçin", key="kullanici",
                     help="Yapılan her işlem bu adla loglanır.")
    else:
        st.text_input("Kullanıcı adı *", key="kullanici", placeholder="Örn: Uğur",
                      help="Yapılan her işlem bu adla loglanır. Yönetim > Ayarlar'dan kullanıcı listesi tanımlanabilir.")
user = ui.current_user()
unread = len(inbox.unread(user)) if user else 0
with st.sidebar:
    if user:
        st.caption(f"🔔 {unread} yeni bildirim" if unread else "🔔 Yeni bildirim yok")
    ui.render_undo_panel()

role = st.session_state["rol"]
sections: dict[str, list[st.Page]] = {
    "İş": [st.Page("views/dashboard.py", title="Yapılacaklar", icon="🏠", default=True)]
          + ([st.Page("views/new_request.py", title="Yeni Talep", icon="➕")] if config.can(role, "talep_gir") else [])
          + [st.Page("views/requests_list.py", title="Talepler", icon="📋")],
    "Takip": [st.Page("views/notifications.py", title=f"Bildirimler ({unread})" if unread else "Bildirimler", icon="🔔"),
              st.Page("views/team_calendar.py", title="Ekip Takvimi", icon="📅"),
              st.Page("views/balances.py", title="Bakiyeler", icon="🧮")],
    "Analiz ve Denetim": [st.Page("views/reports.py", title="Raporlar", icon="📊"),
                          st.Page("views/audit_view.py", title="Audit Trail", icon="🛡️")],
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
except Exception as _e:  # sağlık kontrolü uygulamayı asla düşürmemeli
    storage.log.exception("Bütünlük kontrolü çalışmadı")
    storage.log_event("hata", "saglik", _e)

ui.show_flash()
ui.show_bulk_report()
try:
    nav.run()
except Timeout:
    st.error("Veri dosyası şu an başka bir işlem tarafından kullanılıyor. Birkaç saniye sonra tekrar deneyin.")
except Exception as exc:  # Streamlit'in rerun/stop istisnaları BaseException'dır, buraya düşmez
    err_id = uuid.uuid4().hex[:8]
    storage.log.exception("Beklenmeyen hata [%s]", err_id)
    storage.log_event("hata", err_id, f"{type(exc).__name__}: {exc}")
    st.error(f"Beklenmeyen bir hata oluştu (hata kodu: **{err_id}**). Kayıtlarınız korunmuştur; ayrıntı Yönetim > Sistem durumu'nda.")
    if os.environ.get("IZIN_DEBUG"):
        st.exception(exc)
    if st.button("Sayfayı yenile"):
        st.rerun()
