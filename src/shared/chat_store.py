"""会话存储：多轮对话历史 + 群聊上下文缓冲，SQLite 落盘（data/chat.db）。

与 memory.py 的长期记忆分开：这边是"聊天记录"（/reset、/bot clear all 可清、
按天自动过期），那边是"用户画像"（/bot forget 清）。

- sessions：key -> messages(JSON) + summary（旧对话压缩摘要）。
  key 形如 g:{群号}:{用户} / p:{用户}:{用户}，群与会话天然按 key 隔离。
- group_context：group_id -> lines(JSON)，每群独立一行。

相比旧版 chat_history.json 全量重写，这里每条消息只 UPSERT 自己那一行，
且 updated_at 支撑按天清理。所有函数是同步的（SQLite 本地极快），
异步环境里可直接调用；连接带 threading.Lock，跨线程安全。
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.shared.config import plugin_config
from src.shared.storage import DATA_DIR

logger = logging.getLogger("chat_store")

DB_PATH = DATA_DIR / "chat.db"
_LEGACY_JSON = "chat_history.json"

# 周期性过期清理的最小间隔（秒），插在写入路径里按需触发
_PRUNE_INTERVAL = 6 * 3600

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_last_prune = time.monotonic()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    key        TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    messages   TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_context (
    group_id   INTEGER PRIMARY KEY,
    updated_at TEXT NOT NULL,
    lines      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
"""


def init(path: Path | None = None) -> None:
    """初始化/重置连接；测试用它指向临时库，顺带迁移同目录的旧 JSON。"""
    global _conn, DB_PATH
    if path is not None:
        DB_PATH = Path(path)
    if _conn is not None:
        _conn.close()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    _migrate_add_summary()
    _conn.commit()
    _migrate_legacy()


def _migrate_add_summary() -> None:
    """老库的 sessions 表没有 summary 列，补上（CREATE TABLE 不改旧表）。"""
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "summary" not in cols:
        _conn.execute("ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        init()
    return _conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _migrate_legacy() -> None:
    """把旧版 chat_history.json（与库同目录）搬进 sessions 表，改名 .bak。"""
    legacy = DB_PATH.parent / _LEGACY_JSON
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text("utf-8"))
    except Exception:
        logger.exception("旧聊天记录文件损坏，跳过迁移：%s", legacy)
        return
    now = _now()
    try:
        with _lock:
            for key, messages in data.items():
                if isinstance(key, str) and isinstance(messages, list):
                    _db().execute(
                        "INSERT OR REPLACE INTO sessions(key, updated_at, messages) "
                        "VALUES(?,?,?)",
                        (key, now, json.dumps(messages, ensure_ascii=False)),
                    )
            _db().commit()
        legacy.replace(legacy.parent / (legacy.name + ".bak"))
        logger.info("已迁移旧聊天记录 %d 个会话 -> %s", len(data), DB_PATH.name)
    except Exception:
        logger.exception("迁移旧聊天记录失败：%s", legacy)


# ── 多轮会话（每人每群独立）─────────────────────────────

def load_session_full(key: str) -> tuple[list[dict], str]:
    """返回 (历史消息, 旧对话摘要)；摘要可能为空串。"""
    row = None
    with _lock:
        row = _db().execute(
            "SELECT messages, summary FROM sessions WHERE key=?", (key,)
        ).fetchone()
    if not row:
        return [], ""
    try:
        data = json.loads(row[0])
    except ValueError:
        logger.warning("会话 %s 的历史损坏，按空处理", key)
        data = []
    history = [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []
    return history, str(row[1] or "")


def load_session(key: str) -> list[dict]:
    return load_session_full(key)[0]


def save_session(key: str, messages: list[dict], summary: str = "") -> None:
    with _lock:
        _db().execute(
            "INSERT INTO sessions(key, updated_at, messages, summary) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "updated_at=excluded.updated_at, messages=excluded.messages, "
            "summary=excluded.summary",
            (key, _now(), json.dumps(messages, ensure_ascii=False), summary),
        )
        _db().commit()
    _maybe_prune()


def delete_session(key: str) -> None:
    with _lock:
        _db().execute("DELETE FROM sessions WHERE key=?", (key,))
        _db().commit()


def delete_sessions(prefix: str) -> int:
    """删除所有 key 以 prefix 开头的会话（如 g:{群号}: 清空整群），返回条数。"""
    with _lock:
        cur = _db().execute(
            "DELETE FROM sessions WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        )
        _db().commit()
    return cur.rowcount


def forget_user_sessions(user_id: int) -> int:
    """删除某个用户的所有会话：私聊 p:{uid}:{uid} + 各群里 g:*:{uid}。

    user_id 是整数，拼接模式串没有注入风险。/forget 的隐私承诺要清到
    多轮对话历史，所以和 memory.forget_user 搭配使用。
    """
    uid = str(int(user_id))
    with _lock:
        cur = _db().execute(
            "DELETE FROM sessions WHERE key LIKE ? OR key LIKE ?",
            (f"p:{uid}:%", f"%:{uid}"),
        )
        _db().commit()
    return cur.rowcount


def session_count() -> int:
    with _lock:
        row = _db().execute("SELECT COUNT(*) FROM sessions").fetchone()
    return int(row[0]) if row else 0


# ── 群聊上下文缓冲（每群一份）───────────────────────────

def load_group_lines(group_id: int) -> list[str]:
    row = None
    with _lock:
        row = _db().execute(
            "SELECT lines FROM group_context WHERE group_id=?", (group_id,)
        ).fetchone()
    if not row:
        return []
    try:
        data = json.loads(row[0])
    except ValueError:
        return []
    return [line for line in data if isinstance(line, str)] if isinstance(data, list) else []


def save_group_lines(group_id: int, lines: list[str]) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO group_context(group_id, updated_at, lines) VALUES(?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "updated_at=excluded.updated_at, lines=excluded.lines",
            (group_id, _now(), json.dumps(lines, ensure_ascii=False)),
        )
        _db().commit()
    _maybe_prune()


def delete_group_lines(group_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM group_context WHERE group_id=?", (group_id,))
        _db().commit()


# ── 过期清理 ────────────────────────────────────────────

def prune(keep_days: int) -> int:
    """删除超过 keep_days 天没更新的会话与群上下文，返回清理的行数。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with _lock:
        n1 = _db().execute(
            "DELETE FROM sessions WHERE updated_at < ?", (cutoff,)
        ).rowcount
        n2 = _db().execute(
            "DELETE FROM group_context WHERE updated_at < ?", (cutoff,)
        ).rowcount
        _db().commit()
    return n1 + n2


def _maybe_prune() -> None:
    """写入路径里按需触发的过期清理；关掉 TTL（keep_days<=0）时什么都不做。"""
    global _last_prune
    keep_days = plugin_config.llm_history_ttl_days
    if keep_days <= 0 or time.monotonic() - _last_prune < _PRUNE_INTERVAL:
        return
    _last_prune = time.monotonic()
    try:
        removed = prune(keep_days)
        if removed:
            logger.info("已清理 %d 个超过 %d 天未活跃的会话/群上下文", removed, keep_days)
    except Exception:
        logger.exception("会话过期清理失败")
