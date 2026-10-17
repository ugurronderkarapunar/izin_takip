"""Depolama katmanı.

İki arka uç, aynı arayüz:
  * FileBackend: data/ klasöründe JSON belgeler + JSONL denetim günlüğü (yerel çalışma)
  * SqlBackend : PostgreSQL (Neon, Supabase ...) veya SQLite; bulutta kapat-aç yapınca veri kaybolmaz
Seçim: IZIN_DATABASE_URL (veya Streamlit secrets'taki database_url) doluysa SQL, değilse dosya.

Her yazma bir işlem (transaction) içinde yapılır: hata olursa hepsi geri alınır.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import zlib
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator

from filelock import FileLock

from . import config

log = logging.getLogger("izin")
_WARNINGS: list[str] = []
DOC_NAMES = ["request_db", "personnel", "holidays", "settings", "meta", "anchors", "kullanici_durumu", "bildirim_gecmisi"]
LOCK_KEY = 7364201


def ensure_dirs() -> None:
    for d in (config.DATA_DIR, config.ATTACH_DIR, config.BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    if log.handlers:
        return
    try:
        ensure_dirs()
        handler: logging.Handler = RotatingFileHandler(config.LOG_FILE, maxBytes=500_000, backupCount=3, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def pop_warnings() -> list[str]:
    out = list(_WARNINGS)
    _WARNINGS.clear()
    return out


def _now() -> datetime:
    return datetime.now(config.TZ)


def _stamp(label: str = "") -> str:
    return _now().strftime("%Y-%m-%d_%H%M%S") + (f"_{label}" if label else "")


class Backend:
    kind = "?"

    def read_doc(self, name: str, default: Callable[[], Any]) -> Any: raise NotImplementedError
    def write_doc(self, name: str, data: Any) -> None: raise NotImplementedError
    def audit_read(self) -> list[dict[str, Any]]: raise NotImplementedError
    def audit_last(self) -> dict[str, Any] | None: raise NotImplementedError
    def audit_append(self, entry: dict[str, Any]) -> None: raise NotImplementedError
    def audit_replace_all(self, entries: list[dict[str, Any]]) -> None: raise NotImplementedError
    def put_file(self, talep_id: str, stored: str, data: bytes) -> None: raise NotImplementedError
    def get_file(self, talep_id: str, stored: str) -> bytes: raise NotImplementedError
    def iter_files(self) -> Iterator[tuple[str, str, bytes]]: raise NotImplementedError
    def backup_create(self, label: str = "") -> str: raise NotImplementedError
    def backup_list(self) -> list[str]: raise NotImplementedError
    def backup_restore(self, name: str) -> None: raise NotImplementedError
    def backup_prune(self, keep_days: int) -> int: raise NotImplementedError
    def export_docs(self) -> dict[str, bytes]: raise NotImplementedError
    def event_add(self, level: str, code: str, message: str) -> None: raise NotImplementedError
    def event_list(self, limit: int = 50) -> list[dict[str, Any]]: raise NotImplementedError
    def ping(self) -> str: raise NotImplementedError
    def info(self) -> str: raise NotImplementedError
    def transaction(self): raise NotImplementedError
    def clear_caches(self) -> None: raise NotImplementedError


# ----------------------------------------------------------------------------
# Dosya arka ucu
# ----------------------------------------------------------------------------
class FileBackend(Backend):
    kind = "dosya"

    def __init__(self) -> None:
        self._tls = threading.local()
        self._cache: dict[str, tuple[tuple[int, int], str]] = {}
        self._acache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None
        self._cl = threading.Lock()
        self._lock: FileLock | None = None
        self._lock_init = threading.Lock()
        self._audit_ready = False

    # --- kilit / işlem
    def _flock(self) -> FileLock:
        with self._lock_init:
            if self._lock is None:
                ensure_dirs()
                self._lock = FileLock(str(config.LOCK_FILE), timeout=30)
            return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        t = self._tls
        if getattr(t, "depth", 0) > 0:
            t.depth += 1
            try:
                yield
            finally:
                t.depth -= 1
            return
        with self._flock():
            t.depth = 1
            t.before = {}
            t.audit_size = self._audit_size()
            try:
                yield
            except BaseException:
                self._rollback()
                raise
            finally:
                t.depth = 0

    def _rollback(self) -> None:
        t = self._tls
        for name, raw in t.before.items():
            try:
                p = self._path(name)
                if raw is None:
                    p.unlink(missing_ok=True)
                else:
                    self._atomic_bytes(p, raw)
            except Exception:
                log.exception("Rollback başarısız: %s", name)
        try:
            p = config.AUDIT_JSONL
            if p.exists() and p.stat().st_size > t.audit_size:
                with p.open("r+b") as f:
                    f.truncate(t.audit_size)
        except Exception:
            log.exception("Denetim günlüğü rollback başarısız")
        self.clear_caches()
        log.error("İşlem geri alındı (rollback)")

    def clear_caches(self) -> None:
        with self._cl:
            self._cache.clear()
            self._acache = None

    # --- belgeler
    @staticmethod
    def _path(name: str) -> Path:
        return config.DATA_DIR / f"{name}.json"

    @staticmethod
    def _atomic_bytes(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def read_doc(self, name: str, default: Callable[[], Any]) -> Any:
        ensure_dirs()
        p = self._path(name)
        if not p.exists():
            with self.transaction():
                if not p.exists():
                    self.write_doc(name, default())
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
        with self._cl:
            hit = self._cache.get(name)
        try:
            raw = hit[1] if hit and hit[0] == sig else p.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, type(default())):
                raise ValueError("Beklenmeyen veri tipi")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return self._reset_corrupt(p, default)
        if not hit or hit[0] != sig:
            with self._cl:
                self._cache[name] = (sig, raw)
        return data

    def _reset_corrupt(self, p: Path, default: Callable[[], Any]) -> Any:
        with self.transaction():
            stamp = _now().strftime("%Y%m%d_%H%M%S")
            backup = p.with_name(f"{p.stem}.bozuk_{stamp}{p.suffix}")
            os.replace(p, backup)
            msg = f"{p.name} okunamadı; bozuk dosya '{backup.name}' olarak yedeklendi, boş dosya oluşturuldu."
            _WARNINGS.append(msg)
            log.error(msg)
            data = default()
            self._atomic_bytes(p, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            self.clear_caches()
            return data

    def write_doc(self, name: str, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        with self.transaction():
            p = self._path(name)
            t = self._tls
            if name not in t.before:
                t.before[name] = p.read_bytes() if p.exists() else None
            self._atomic_bytes(p, raw)
            st = p.stat()
            with self._cl:
                self._cache[name] = ((st.st_mtime_ns, st.st_size), raw.decode("utf-8"))

    # --- denetim günlüğü (JSONL: satır satır ekleme)
    def _audit_size(self) -> int:
        p = config.AUDIT_JSONL
        return p.stat().st_size if p.exists() else 0

    def _audit_init(self) -> None:
        if self._audit_ready:
            return
        ensure_dirs()
        p, legacy = config.AUDIT_JSONL, config.AUDIT_LOG
        with self.transaction():
            if not p.exists() and legacy.exists():  # eski sürüm: tek JSON dizisi -> JSONL
                try:
                    entries = json.loads(legacy.read_text(encoding="utf-8"))
                    if isinstance(entries, list):
                        self._atomic_bytes(p, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode("utf-8"))
                        legacy.rename(legacy.with_name(legacy.name + ".migrated"))
                        log.info("audit_logs.json -> audit_logs.jsonl dönüştürüldü (%d kayıt)", len(entries))
                except (json.JSONDecodeError, OSError):
                    log.exception("Eski denetim günlüğü dönüştürülemedi")
            if not p.exists():
                p.touch()
            self._repair_tail()
        self._audit_ready = True

    @staticmethod
    def _repair_tail() -> None:
        """Yarım yazılmış son satır (çökme) varsa atar."""
        p = config.AUDIT_JSONL
        size = p.stat().st_size
        if size == 0:
            return
        with p.open("r+b") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b"\n":
                return
            f.seek(0)
            data = f.read()
            keep = data.rfind(b"\n") + 1
            f.truncate(keep)
        _WARNINGS.append("Denetim günlüğünün yarım yazılmış son satırı temizlendi.")
        log.warning("audit_logs.jsonl son satır onarıldı")

    def audit_read(self) -> list[dict[str, Any]]:
        self._audit_init()
        p = config.AUDIT_JSONL
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
        with self._cl:
            c = self._acache
        if c and c[0] == sig:
            return list(c[1])
        entries: list[dict[str, Any]] = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                _WARNINGS.append(f"Denetim günlüğünde {i}. satır okunamadı (atlandı).")
        with self._cl:
            self._acache = (sig, entries)
        return list(entries)

    def audit_last(self) -> dict[str, Any] | None:
        self._audit_init()
        p = config.AUDIT_JSONL
        size = p.stat().st_size
        if size == 0:
            return None
        buf = b""
        with p.open("rb") as f:
            pos = size
            while pos > 0:
                step = min(8192, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                stripped = buf.rstrip(b"\n")
                if b"\n" in stripped:
                    return json.loads(stripped.rsplit(b"\n", 1)[1])
        return json.loads(buf.rstrip(b"\n"))

    def audit_append(self, entry: dict[str, Any]) -> None:
        with self.transaction():
            self._audit_init()
            line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            with config.AUDIT_JSONL.open("ab") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            with self._cl:
                self._acache = None

    def audit_replace_all(self, entries: list[dict[str, Any]]) -> None:
        with self.transaction():
            self._audit_init()
            self._atomic_bytes(config.AUDIT_JSONL, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode("utf-8"))
            with self._cl:
                self._acache = None

    # --- ekler
    def put_file(self, talep_id: str, stored: str, data: bytes) -> None:
        folder = config.ATTACH_DIR / talep_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / stored).write_bytes(data)

    def get_file(self, talep_id: str, stored: str) -> bytes:
        return (config.ATTACH_DIR / talep_id / Path(stored).name).read_bytes()

    def iter_files(self) -> Iterator[tuple[str, str, bytes]]:
        if config.ATTACH_DIR.exists():
            for p in config.ATTACH_DIR.rglob("*"):
                if p.is_file():
                    yield p.parent.name, p.name, p.read_bytes()

    # --- yedek
    def backup_create(self, label: str = "") -> str:
        name = _stamp(label)
        dest = config.BACKUP_DIR / name
        dest.mkdir(parents=True, exist_ok=True)
        with self.transaction():
            for n in DOC_NAMES:
                if self._path(n).exists():
                    (dest / f"{n}.json").write_bytes(self._path(n).read_bytes())
            if config.AUDIT_JSONL.exists():
                (dest / "audit_logs.jsonl").write_bytes(config.AUDIT_JSONL.read_bytes())
        return name

    def backup_list(self) -> list[str]:
        if not config.BACKUP_DIR.exists():
            return []
        return sorted((p.name for p in config.BACKUP_DIR.iterdir() if p.is_dir()), reverse=True)

    def backup_restore(self, name: str) -> None:
        src = config.BACKUP_DIR / name
        if name != Path(name).name or not src.is_dir():
            raise ValueError("Yedek bulunamadı.")
        self.backup_create("geri_yukleme_oncesi")
        with self.transaction():
            for n in DOC_NAMES:
                if (src / f"{n}.json").exists():
                    self._tls.before.setdefault(n, self._path(n).read_bytes() if self._path(n).exists() else None)
                    self._atomic_bytes(self._path(n), (src / f"{n}.json").read_bytes())
            self._audit_init()
            if (src / "audit_logs.jsonl").exists():
                self._atomic_bytes(config.AUDIT_JSONL, (src / "audit_logs.jsonl").read_bytes())
            elif (src / "audit_logs.json").exists():  # eski sürüm yedeği
                entries = json.loads((src / "audit_logs.json").read_text(encoding="utf-8"))
                self.audit_replace_all(entries)
            self.clear_caches()

    def backup_prune(self, keep_days: int) -> int:
        import shutil
        cutoff = (_now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        removed = 0
        for name in self.backup_list():
            if name[:10] < cutoff:
                shutil.rmtree(config.BACKUP_DIR / name, ignore_errors=True)
                removed += 1
        return removed

    def export_docs(self) -> dict[str, bytes]:
        out = {f"{n}.json": self._path(n).read_bytes() for n in DOC_NAMES if self._path(n).exists()}
        if config.AUDIT_JSONL.exists():
            out["audit_logs.jsonl"] = config.AUDIT_JSONL.read_bytes()
        return out

    # --- olaylar (gözlem)
    def event_add(self, level: str, code: str, message: str) -> None:
        ensure_dirs()
        p = config.DATA_DIR / "events.jsonl"
        if p.exists() and p.stat().st_size > 512_000:
            p.replace(p.with_name("events.jsonl.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "code": code, "message": message}, ensure_ascii=False) + "\n")

    def event_list(self, limit: int = 50) -> list[dict[str, Any]]:
        p = config.DATA_DIR / "events.jsonl"
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[::-1]

    def ping(self) -> str:
        ensure_dirs()
        return "Dosya sistemi erişilebilir"

    def info(self) -> str:
        return f"Dosya depolama: {config.DATA_DIR}"


# ----------------------------------------------------------------------------
# SQL arka ucu (PostgreSQL / SQLite)
# ----------------------------------------------------------------------------
def _normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class SqlBackend(Backend):
    kind = "veritabanı"

    def __init__(self, url: str) -> None:
        import sqlalchemy as sa
        self.sa = sa
        url = _normalize_url(url)
        kw: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:  # pgbouncer (işlem havuzu) ile uyumlu: sunucu tarafı hazır ifade yok
            kw["connect_args"] = {"prepare_threshold": None}
            kw.update(pool_size=3, max_overflow=2, pool_recycle=240, pool_pre_ping=True)
        self.engine = sa.create_engine(url, **kw)
        self.is_pg = self.engine.dialect.name == "postgresql"
        meta = sa.MetaData()
        self.kv = sa.Table("kv_docs", meta, sa.Column("name", sa.String(100), primary_key=True), sa.Column("data", sa.Text, nullable=False),
                           sa.Column("version", sa.Integer, nullable=False), sa.Column("updated_at", sa.String(32)))
        self.al = sa.Table("audit_logs", meta, sa.Column("log_id", sa.Integer, primary_key=True, autoincrement=False), sa.Column("data", sa.Text, nullable=False))
        self.att = sa.Table("attachments", meta, sa.Column("talep_id", sa.String(40), primary_key=True), sa.Column("stored", sa.String(200), primary_key=True),
                            sa.Column("data", sa.LargeBinary, nullable=False))
        self.bk = sa.Table("backups", meta, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True), sa.Column("name", sa.String(100), unique=True),
                           sa.Column("created", sa.String(32)), sa.Column("payload", sa.LargeBinary, nullable=False))
        self.ev = sa.Table("app_events", meta, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True), sa.Column("ts", sa.String(32)),
                           sa.Column("level", sa.String(16)), sa.Column("code", sa.String(32)), sa.Column("message", sa.Text))
        meta.create_all(self.engine)
        self._tls = threading.local()
        self._cache: dict[str, tuple[int, str]] = {}
        self._acache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None
        self._cl = threading.Lock()
        self._lock: FileLock | None = None
        self._lock_init = threading.Lock()

    # --- işlem
    def _flock(self):
        with self._lock_init:
            if self._lock is None:
                path = self.engine.url.database or "izin.db"
                self._lock = FileLock(f"{path}.lock", timeout=30)
            return self._lock

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        c = getattr(self._tls, "conn", None)
        if c is not None:
            yield c
        else:
            with self.engine.connect() as conn:
                yield conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        t = self._tls
        if getattr(t, "conn", None) is not None:
            yield
            return
        written: set[str] = set()
        with (nullcontext() if self.is_pg else self._flock()):
            try:
                with self.engine.begin() as conn:
                    if self.is_pg:  # işlem bitince otomatik açılan, süreçler/sunucular arası kilit
                        conn.execute(self.sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
                    t.conn, t.written = conn, written
                    try:
                        yield
                    finally:
                        t.conn, t.written = None, set()
            except BaseException:
                self._drop(written, audit=True)
                log.error("İşlem geri alındı (rollback)")
                raise
            else:
                self._drop(written, audit=False)

    def _drop(self, names: set[str], audit: bool) -> None:
        with self._cl:
            for n in names:
                self._cache.pop(n, None)
            if audit:
                self._acache = None

    def clear_caches(self) -> None:
        with self._cl:
            self._cache.clear()
            self._acache = None

    # --- belgeler
    def read_doc(self, name: str, default: Callable[[], Any]) -> Any:
        sa, kv = self.sa, self.kv
        with self._conn() as conn:
            ver = conn.execute(sa.select(kv.c.version).where(kv.c.name == name)).scalar()
            if ver is not None:
                written = getattr(self._tls, "written", set())
                with self._cl:
                    hit = self._cache.get(name)
                if hit and hit[0] == ver and name not in written:
                    return json.loads(hit[1])
                raw = conn.execute(sa.select(kv.c.data).where(kv.c.name == name)).scalar()
                if name not in written:
                    with self._cl:
                        self._cache[name] = (ver, raw)
                return json.loads(raw)
        self.write_doc(name, default())  # ilk kullanım: boş belge oluştur
        return default()

    def write_doc(self, name: str, data: Any) -> None:
        sa, kv = self.sa, self.kv
        raw = json.dumps(data, ensure_ascii=False)
        with self.transaction():
            conn, ts = self._tls.conn, _now().strftime("%Y-%m-%d %H:%M:%S")
            res = conn.execute(sa.update(kv).where(kv.c.name == name).values(data=raw, version=kv.c.version + 1, updated_at=ts))
            if res.rowcount == 0:
                conn.execute(sa.insert(kv).values(name=name, data=raw, version=1, updated_at=ts))
            self._tls.written.add(name)

    # --- denetim günlüğü (yalnızca ekleme)
    def audit_read(self) -> list[dict[str, Any]]:
        sa, al = self.sa, self.al
        with self._conn() as conn:
            sig = tuple(conn.execute(sa.select(sa.func.count(), sa.func.max(al.c.log_id))).one())
            with self._cl:
                c = self._acache
            if c and c[0] == sig:
                return list(c[1])
            rows = conn.execute(sa.select(al.c.data).order_by(al.c.log_id)).scalars().all()
            entries = [json.loads(r) for r in rows]
            with self._cl:
                self._acache = (sig, entries)
            return list(entries)

    def audit_last(self) -> dict[str, Any] | None:
        sa, al = self.sa, self.al
        with self._conn() as conn:
            raw = conn.execute(sa.select(al.c.data).order_by(al.c.log_id.desc()).limit(1)).scalar()
        return json.loads(raw) if raw else None

    def audit_append(self, entry: dict[str, Any]) -> None:
        with self.transaction():
            self._tls.conn.execute(self.sa.insert(self.al).values(log_id=entry["log_id"], data=json.dumps(entry, ensure_ascii=False)))

    def audit_replace_all(self, entries: list[dict[str, Any]]) -> None:
        with self.transaction():
            conn = self._tls.conn
            conn.execute(self.sa.delete(self.al))
            if entries:
                conn.execute(self.sa.insert(self.al), [{"log_id": e["log_id"], "data": json.dumps(e, ensure_ascii=False)} for e in entries])
        self.clear_caches()

    # --- ekler
    def put_file(self, talep_id: str, stored: str, data: bytes) -> None:
        sa, att = self.sa, self.att
        with self.transaction():
            conn = self._tls.conn
            conn.execute(sa.delete(att).where(att.c.talep_id == talep_id, att.c.stored == stored))
            conn.execute(sa.insert(att).values(talep_id=talep_id, stored=stored, data=data))

    def get_file(self, talep_id: str, stored: str) -> bytes:
        sa, att = self.sa, self.att
        with self._conn() as conn:
            raw = conn.execute(sa.select(att.c.data).where(att.c.talep_id == talep_id, att.c.stored == Path(stored).name)).scalar()
        if raw is None:
            raise FileNotFoundError(stored)
        return bytes(raw)

    def iter_files(self) -> Iterator[tuple[str, str, bytes]]:
        with self._conn() as conn:
            for tid, stored, data in conn.execute(self.sa.select(self.att.c.talep_id, self.att.c.stored, self.att.c.data)):
                yield tid, stored, bytes(data)

    # --- yedek (veritabanı içinde sıkıştırılmış anlık görüntü)
    def backup_create(self, label: str = "") -> str:
        sa, kv, bk = self.sa, self.kv, self.bk
        name = _stamp(label)
        with self.transaction():
            conn = self._tls.conn
            docs = {n: d for n, d in conn.execute(sa.select(kv.c.name, kv.c.data)) if n in DOC_NAMES}
            payload = zlib.compress(json.dumps({"docs": docs, "audit": self.audit_read()}, ensure_ascii=False).encode("utf-8"))
            conn.execute(sa.insert(bk).values(name=name, created=_now().strftime("%Y-%m-%d %H:%M:%S"), payload=payload))
        return name

    def backup_list(self) -> list[str]:
        with self._conn() as conn:
            return list(conn.execute(self.sa.select(self.bk.c.name).order_by(self.bk.c.id.desc())).scalars().all())

    def backup_restore(self, name: str) -> None:
        sa, bk = self.sa, self.bk
        with self._conn() as conn:
            payload = conn.execute(sa.select(bk.c.payload).where(bk.c.name == name)).scalar()
        if payload is None:
            raise ValueError("Yedek bulunamadı.")
        snap = json.loads(zlib.decompress(bytes(payload)).decode("utf-8"))
        self.backup_create("geri_yukleme_oncesi")
        with self.transaction():
            for n, raw in snap["docs"].items():
                self.write_doc(n, json.loads(raw))
            self.audit_replace_all(snap["audit"])
        self.clear_caches()

    def backup_prune(self, keep_days: int) -> int:
        cutoff = (_now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        with self.transaction():
            res = self._tls.conn.execute(self.sa.delete(self.bk).where(self.bk.c.created < cutoff))
            return res.rowcount or 0

    def export_docs(self) -> dict[str, bytes]:
        with self._conn() as conn:
            out = {f"{n}.json": d.encode("utf-8") for n, d in conn.execute(self.sa.select(self.kv.c.name, self.kv.c.data)) if n in DOC_NAMES}
        out["audit_logs.jsonl"] = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.audit_read()).encode("utf-8")
        return out

    # --- olaylar (gözlem)
    def event_add(self, level: str, code: str, message: str) -> None:
        with self.transaction():
            self._tls.conn.execute(self.sa.insert(self.ev).values(ts=_now().strftime("%Y-%m-%d %H:%M:%S"), level=level, code=code, message=message))

    def event_list(self, limit: int = 50) -> list[dict[str, Any]]:
        ev = self.ev
        with self._conn() as conn:
            rows = conn.execute(self.sa.select(ev.c.ts, ev.c.level, ev.c.code, ev.c.message).order_by(ev.c.id.desc()).limit(limit)).all()
        return [{"ts": r[0], "level": r[1], "code": r[2], "message": r[3]} for r in rows]

    def ping(self) -> str:
        with self._conn() as conn:
            conn.execute(self.sa.text("SELECT 1"))
        return f"{self.engine.dialect.name} bağlantısı tamam"

    def info(self) -> str:
        return f"Veritabanı: {self.engine.url.render_as_string(hide_password=True)}"


# ----------------------------------------------------------------------------
# Seçim ve yardımcılar
# ----------------------------------------------------------------------------
_backend: Backend | None = None
_backend_lock = threading.Lock()


def get_backend() -> Backend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                url = config.database_url()
                _backend = SqlBackend(url) if url else FileBackend()
    return _backend


def use_backend(backend: Backend | None) -> None:
    """Testler ve araçlar için arka ucu elle değiştirir."""
    global _backend
    _backend = backend


@contextmanager
def transaction() -> Iterator[None]:
    """Tüm yazmaları kapsayan işlem: hata olursa her şey geri alınır."""
    with get_backend().transaction():
        yield


data_lock = transaction  # eski ad


class JsonStore:
    """Tek bir belge (liste ya da sözlük). Ad, dosya adı kökünden (request_db.json -> 'request_db') alınır."""

    def __init__(self, path_or_name: Path | str, default: Callable[[], Any] = list) -> None:
        self.name = Path(path_or_name).stem if isinstance(path_or_name, Path) else str(path_or_name)
        self.default = default

    def read(self) -> Any:
        return get_backend().read_doc(self.name, self.default)

    def write(self, data: Any) -> None:
        get_backend().write_doc(self.name, data)


def clear_cache() -> None:
    get_backend().clear_caches()


def log_event(level: str, code: str, message: Any) -> None:
    """Gözlem için olay kaydı; asla uygulamayı düşürmez."""
    try:
        get_backend().event_add(level, code, str(message)[:2000])
    except Exception:
        log.exception("Olay kaydedilemedi")


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    try:
        return get_backend().event_list(limit)
    except Exception:
        log.exception("Olaylar okunamadı")
        return []


meta_store = JsonStore("meta", dict)

# END-OF-FILE v3.0
