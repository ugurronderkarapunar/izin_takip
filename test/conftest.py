import os
import sys
import tempfile
from pathlib import Path

# Veri klasörü, çekirdek modüller import edilmeden ÖNCE geçici klasöre yönlendirilir.
_TMP = tempfile.mkdtemp(prefix="izin_test_")
os.environ["IZIN_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core import bootstrap  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init():
    bootstrap.init()
