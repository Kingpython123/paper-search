"""SQLite 缓存。

原项目每查一篇论文就重新请求 letpub，同一个期刊被反复问；
这里按命名空间 + 键缓存 JSON，带 TTL，重复查询几乎零成本，
也顺带把 OpenAlex 的每日预算省下来。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL = 30 * 24 * 3600  # 期刊指标变化慢，缓存一个月足够

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    ns      TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (ns, key)
);
CREATE INDEX IF NOT EXISTS idx_kv_ts ON kv(ts);
"""


class Cache:
    """线程安全的键值缓存。

    用 check_same_thread=False + 一把锁，而不是每次开新连接，
    因为 GUI 场景下工作线程和主线程都可能访问。
    """

    def __init__(self, path: str | Path | None = None, ttl: int = DEFAULT_TTL,
                 enabled: bool = True) -> None:
        self.ttl = ttl
        self.enabled = enabled
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if not enabled:
            return
        if path is None:
            path = Path.home() / ".journal_picker" / "cache.sqlite3"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ 读写

    def get(self, ns: str, key: str) -> Any | None:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT value, ts FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        if not row:
            return None
        value, ts = row
        if self.ttl > 0 and time.time() - ts > self.ttl:
            self.delete(ns, key)
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            # 缓存内容损坏时按未命中处理，不能让脏数据把整个流程带崩
            self.delete(ns, key)
            return None

    def set(self, ns: str, key: str, value: Any) -> None:
        if not self.enabled or self._conn is None:
            return
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (ns, key, value, ts) VALUES (?,?,?,?)",
                (ns, key, payload, time.time()))
            self._conn.commit()

    def get_many(self, ns: str, keys: list[str]) -> dict[str, Any]:
        """批量取，用于「先查缓存、只对未命中的发请求」这种模式。"""
        if not self.enabled or self._conn is None or not keys:
            return {}
        out: dict[str, Any] = {}
        # SQLite 单条语句参数上限约 999，分批处理
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT key, value, ts FROM kv WHERE ns=? AND key IN ({marks})",
                    (ns, *chunk)).fetchall()
            now = time.time()
            for key, value, ts in rows:
                if self.ttl > 0 and now - ts > self.ttl:
                    continue
                try:
                    out[key] = json.loads(value)
                except json.JSONDecodeError:
                    continue
        return out

    def delete(self, ns: str, key: str) -> None:
        if not self.enabled or self._conn is None:
            return
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))
            self._conn.commit()

    # ------------------------------------------------------------------ 维护

    def purge_expired(self) -> int:
        if not self.enabled or self._conn is None or self.ttl <= 0:
            return 0
        cutoff = time.time() - self.ttl
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount or 0

    def stats(self) -> dict[str, int]:
        if not self.enabled or self._conn is None:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT ns, COUNT(*) FROM kv GROUP BY ns").fetchall()
        return {ns: n for ns, n in rows}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class NullCache(Cache):
    """显式关闭缓存时用，省得到处判 None。"""

    def __init__(self) -> None:
        super().__init__(enabled=False)
