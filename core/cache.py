"""Kısa ömürlü önbellek: sık okunan verileri (ayarlar, personel, tatiller, talepler) yeniden okumayı/çözmeyi önler.

Güvenlik kuralları (veri kaybı olmaması için):
  * İşlem (transaction) içinde ASLA önbellek kullanılmaz; yazma öncesi okumalar her zaman güncel veridir.
  * Her yazma / geri alma 'nesil' sayacını artırır; sayaç değişince önbellek geçersiz olur.
  * Başka bir süreç yazarsa en geç TTL saniye içinde görünür (3 kişilik ekip için yeterli).
"""
from __future__ import annotations

import copy
import functools
import threading
import time
from typing import Any, Callable

from . import storage

TTL = 3.0


def memo(fn: Callable[..., Any]) -> Callable[..., Any]:
    state: dict[Any, tuple[float, Any]] = {}
    lock = threading.Lock()

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if storage.in_transaction():
            return fn(*args, **kwargs)
        gen = storage.generation()
        key = (gen, args, tuple(sorted(kwargs.items())))
        now = time.monotonic()
        with lock:
            for k in [k for k in state if k[0] != gen]:
                del state[k]
            hit = state.get(key)
        if hit and now - hit[0] < TTL:
            return copy.deepcopy(hit[1])
        val = fn(*args, **kwargs)
        with lock:
            state[key] = (now, val)
        return copy.deepcopy(val)

    wrapper.cache_clear = lambda: state.clear()  # type: ignore[attr-defined]
    return wrapper

# END-OF-FILE v3.2
