import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from core import diagnostics
from tests.test_ui import ROOT


@pytest.mark.parametrize("msg,expect", [
    ('connection failed: FATAL: password authentication failed for user "neondb_owner"', "parola"),
    ("could not translate host name \"ep-x.neon.tech\" to address: Name or service not known", "Sunucu adı"),
    ("connection to server at \"db.x.supabase.co\" failed: Network is unreachable", "Session pooler"),
    ("connection refused", "reddedildi"),
    ("timeout expired", "uyur"),
    ("Could not parse SQLAlchemy URL from string 'psql postgresql://x'", "EN ÜSTÜNDE"),
    ("No module named 'psycopg'", "sürücü"),
])
def test_hints(msg, expect):
    d = diagnostics.describe_error(RuntimeError(msg))
    assert expect.lower() in d["ipucu"].lower()


def test_password_never_leaks():
    msg = 'connection to "postgresql://neondb_owner:S3cretPass@ep-x.neon.tech/db" failed password=S3cretPass'
    out = diagnostics.describe_error(RuntimeError(msg))["mesaj"]
    assert "S3cretPass" not in out and "***" in out


def test_permanent_errors_are_not_retried():
    assert diagnostics.is_permanent(RuntimeError("password authentication failed"))
    assert not diagnostics.is_permanent(RuntimeError("timeout expired"))


def test_app_shows_cause_without_password(monkeypatch):
    monkeypatch.setenv("IZIN_DATABASE_URL", "postgresql://kullanici:GizliParola@127.0.0.1:1/veritabani")
    monkeypatch.setenv("IZIN_DB_RETRIES", "1")
    from core import storage
    storage.use_backend(None)
    st.cache_resource.clear()          # _init önceki testlerde önbelleğe alınmış olabilir
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    try:
        assert "Veri deposuna bağlanılamadı" in at.error[0].value
        text = " ".join(c.value for c in at.code) + " ".join(m.value for m in at.markdown)
        assert "127.0.0.1" in text and "GizliParola" not in text and "Olası neden" in text
        assert any(b.label == "Tekrar dene" for b in at.button)
    finally:
        monkeypatch.delenv("IZIN_DATABASE_URL")
        storage.use_backend(None)
        st.cache_resource.clear()
