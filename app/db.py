"""SQLite-Speicher: Warteschlange, Verlauf, TMDB-Cache."""
import json
import os
import sqlite3
import threading
import time

_lock = threading.RLock()
_conn = None


def init(path):
    global _conn
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size INTEGER,
                mtime REAL,
                parsed TEXT,
                match TEXT,
                target TEXT,
                confidence REAL DEFAULT 0,
                status TEXT DEFAULT 'new',
                message TEXT,
                updated REAL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                action TEXT,
                src TEXT,
                dst TEXT,
                kind TEXT,
                title TEXT,
                extras TEXT,
                undone INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trash (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                user TEXT,
                orig TEXT,
                stored TEXT,
                is_dir INTEGER,
                size INTEGER
            );
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                user TEXT,
                package TEXT,
                count INTEGER,
                urls TEXT
            );
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                ts REAL
            );
            """
        )
        _conn.commit()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("parsed", "match", "extras"):
        if k in d and d[k]:
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


# ---------- Items ----------
def upsert_item(item):
    with _lock:
        _conn.execute(
            """INSERT INTO items (id, path, size, mtime, parsed, match, target, confidence, status, message, updated)
               VALUES (:id, :path, :size, :mtime, :parsed, :match, :target, :confidence, :status, :message, :updated)
               ON CONFLICT(id) DO UPDATE SET
                 path=excluded.path, size=excluded.size, mtime=excluded.mtime, parsed=excluded.parsed,
                 match=excluded.match, target=excluded.target, confidence=excluded.confidence,
                 status=excluded.status, message=excluded.message, updated=excluded.updated""",
            {
                "id": item["id"],
                "path": item["path"],
                "size": item.get("size"),
                "mtime": item.get("mtime"),
                "parsed": json.dumps(item.get("parsed")),
                "match": json.dumps(item.get("match")),
                "target": item.get("target"),
                "confidence": item.get("confidence", 0),
                "status": item.get("status", "new"),
                "message": item.get("message"),
                "updated": time.time(),
            },
        )
        _conn.commit()


def get_item(item_id):
    with _lock:
        return _row(_conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())


def list_items(include_done=False):
    q = "SELECT * FROM items"
    if not include_done:
        q += " WHERE status NOT IN ('done','ignored')"
    q += " ORDER BY path COLLATE NOCASE"
    with _lock:
        return [_row(r) for r in _conn.execute(q).fetchall()]


def set_status(item_id, status, message=None):
    with _lock:
        _conn.execute(
            "UPDATE items SET status=?, message=?, updated=? WHERE id=?",
            (status, message, time.time(), item_id),
        )
        _conn.commit()


def delete_item(item_id):
    with _lock:
        _conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        _conn.commit()


def all_item_ids():
    with _lock:
        return {r[0]: (r[1], r[2], r[3]) for r in _conn.execute("SELECT id, status, size, mtime FROM items")}


# ---------- History ----------
def add_history(action, src, dst, kind, title, extras):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO history (ts, action, src, dst, kind, title, extras) VALUES (?,?,?,?,?,?,?)",
            (time.time(), action, src, dst, kind, title, json.dumps(extras or [])),
        )
        _conn.commit()
        return cur.lastrowid


def list_history(limit=200):
    with _lock:
        return [
            _row(r)
            for r in _conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        ]


def get_history(hid):
    with _lock:
        return _row(_conn.execute("SELECT * FROM history WHERE id=?", (hid,)).fetchone())


def mark_undone(hid):
    with _lock:
        _conn.execute("UPDATE history SET undone=1 WHERE id=?", (hid,))
        _conn.commit()


def stats():
    with _lock:
        today = time.time() - 86400
        total = _conn.execute("SELECT COUNT(*) FROM history WHERE undone=0").fetchone()[0]
        day = _conn.execute("SELECT COUNT(*) FROM history WHERE undone=0 AND ts>?", (today,)).fetchone()[0]
        by_status = dict(_conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        return {"total": total, "last24h": day, "items": by_status}


# ---------- Cache ----------
def cache_get(key, max_age):
    with _lock:
        r = _conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r[1] < max_age:
        return json.loads(r[0])
    return None


def cache_set(key, value):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, ts) VALUES (?,?,?)",
            (key, json.dumps(value), time.time()),
        )
        _conn.commit()


def cache_clear():
    with _lock:
        _conn.execute("DELETE FROM cache")
        _conn.commit()


def add_trash(user, orig, stored, is_dir, size):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO trash (ts, user, orig, stored, is_dir, size) VALUES (?,?,?,?,?,?)",
            (time.time(), user, orig, stored, 1 if is_dir else 0, size),
        )
        _conn.commit()
        return cur.lastrowid


def list_trash():
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM trash ORDER BY ts DESC")]


def get_trash(tid):
    with _lock:
        r = _conn.execute("SELECT * FROM trash WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


def delete_trash(tid):
    with _lock:
        _conn.execute("DELETE FROM trash WHERE id=?", (tid,))
        _conn.commit()


def add_links(user, package, urls):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO links (ts, user, package, count, urls) VALUES (?,?,?,?,?)",
            (time.time(), user, package, len(urls), json.dumps(urls)),
        )
        _conn.commit()
        return cur.lastrowid


def list_links(limit=30):
    with _lock:
        rows = _conn.execute("SELECT * FROM links ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["urls"] = json.loads(d["urls"] or "[]")
        out.append(d)
    return out
