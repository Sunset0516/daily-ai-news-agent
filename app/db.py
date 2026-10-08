"""SQLite 数据访问层：初始化建表 + 用户/偏好/简报的 CRUD。

设计：每次操作新建短连接（sqlite3 默认禁止跨线程共享连接），
     用 contextmanager 自动 PRAGMA foreign_keys + 提交/回滚 + 关闭。
     写操作加 threading.Lock 串行化，避免 SQLite 写锁冲突。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

from app.config import DB_PATH, SCHEMA_PATH

# 写操作串行化锁
_lock = threading.Lock()


@contextmanager
def get_conn():
    """获取一个短连接：开外键 + 提交/回滚 + 关闭。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """执行 schema.sql 建表（幂等）。启动时调用一次。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(sql)


# ---------- 用户 ----------

def create_user(name: str, feishu_webhook: Optional[str] = None) -> Optional[dict]:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users(name, feishu_webhook) VALUES(?, ?)",
            (name, feishu_webhook),
        )
        user_id = cur.lastrowid
    return get_user(user_id)


def get_user(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY user_id")]


def update_user(user_id: int, name: Optional[str] = None,
                feishu_webhook: Optional[str] = None) -> Optional[dict]:
    fields, vals = [], []
    if name is not None:
        fields.append("name=?")
        vals.append(name)
    if feishu_webhook is not None:
        fields.append("feishu_webhook=?")
        vals.append(feishu_webhook)
    if not fields:
        return get_user(user_id)
    vals.append(user_id)
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", vals)
    return get_user(user_id)


def delete_user(user_id: int) -> None:
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))


# ---------- 偏好 ----------

def add_preference(user_id: int, type_: str, value: str) -> Optional[dict]:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO preferences(user_id, type, value) VALUES(?, ?, ?)",
            (user_id, type_, value),
        )
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM preferences WHERE user_id=? AND type=? AND value=? "
            "ORDER BY pref_id DESC LIMIT 1",
            (user_id, type_, value),
        ).fetchone()
        return dict(row) if row else None


def list_preferences(user_id: int) -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM preferences WHERE user_id=? ORDER BY pref_id", (user_id,))]


def delete_preference(pref_id: int) -> None:
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM preferences WHERE pref_id=?", (pref_id,))


# ---------- 简报 ----------

def save_briefing(user_id: int, briefing_date, title: str, summary: str,
                  markdown_content: str, file_path: str, feishu_pushed: int,
                  push_error: Optional[str], tool_call_count: int,
                  agent_log: list) -> Optional[dict]:
    """保存简报（一人一天一份，UPSERT 覆盖当日）。"""
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO briefings(user_id, briefing_date, title, summary, markdown_content,
                   file_path, feishu_pushed, push_error, tool_call_count, agent_log)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, briefing_date) DO UPDATE SET
                   title=excluded.title, summary=excluded.summary,
                   markdown_content=excluded.markdown_content, file_path=excluded.file_path,
                   feishu_pushed=excluded.feishu_pushed, push_error=excluded.push_error,
                   tool_call_count=excluded.tool_call_count, agent_log=excluded.agent_log""",
            (user_id, str(briefing_date), title, summary, markdown_content, file_path,
             feishu_pushed, push_error, tool_call_count,
             json.dumps(agent_log, ensure_ascii=False)),
        )
    return get_briefing_by_user_date(user_id, briefing_date)


def get_briefing(briefing_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM briefings WHERE briefing_id=?", (briefing_id,)).fetchone()
        return _decode_briefing(row)


def get_briefing_by_user_date(user_id: int, briefing_date) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM briefings WHERE user_id=? AND briefing_date=?",
            (user_id, str(briefing_date))).fetchone()
        return _decode_briefing(row)


def list_briefings(user_id: int) -> list:
    """列表只取摘要字段，不返回完整 markdown/agent_log（前端列表用）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT briefing_id, user_id, briefing_date, title, summary, file_path,
                      feishu_pushed, push_error, tool_call_count, created_at
               FROM briefings WHERE user_id=? ORDER BY briefing_date DESC""",
            (user_id,)).fetchall()
        return [dict(r) for r in rows]


def _decode_briefing(row) -> Optional[dict]:
    """把 agent_log 字段从 JSON 字符串解码成 list。"""
    if not row:
        return None
    d = dict(row)
    if d.get("agent_log"):
        try:
            d["agent_log"] = json.loads(d["agent_log"])
        except (ValueError, TypeError):
            pass
    return d
