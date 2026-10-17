"""Test ortamı. Arka uç seçimi: IZIN_TEST_BACKEND = file (varsayılan) | sqlite | pgserver | postgres.

  file      : JSON + JSONL dosyaları (geçici klasör)
  sqlite    : SQLAlchemy + SQLite
  pgserver  : gömülü gerçek PostgreSQL (pip install pgserver) — yerelde PostgreSQL denemek için
  postgres  : IZIN_TEST_PG_URL ile verilen PostgreSQL (CI servis konteyneri)
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="izin_test_")
os.environ["IZIN_DATA_DIR"] = _TMP
BACKEND = os.environ.get("IZIN_TEST_BACKEND", "file")
os.environ.pop("IZIN_DATABASE_URL", None)
_PG = None
if BACKEND == "sqlite":
    os.environ["IZIN_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
elif BACKEND == "pgserver":
    import pgserver
    _PG = pgserver.get_server(Path(_TMP) / "pg")
    os.environ["IZIN_DATABASE_URL"] = _PG.get_uri()
elif BACKEND == "postgres":
    os.environ["IZIN_DATABASE_URL"] = os.environ["IZIN_TEST_PG_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core import bootstrap  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init():
    bootstrap.init()
    yield
    if _PG is not None:
        _PG.cleanup()


@pytest.fixture(scope="session")
def backend_kind() -> str:
    return BACKEND


file_only = pytest.mark.skipif(BACKEND != "file", reason="yalnızca dosya arka ucu")
