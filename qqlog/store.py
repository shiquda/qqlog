"""Index database: normalized messages, FTS, and query helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    rowid     INTEGER PRIMARY KEY,
    chat_type TEXT NOT NULL,
    chat_id   TEXT NOT NULL,
    msg_id    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    sender_qq TEXT,
    sender_uid TEXT,
    direction INTEGER,
    kind      TEXT,
    text      TEXT,
    UNIQUE(chat_type, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_type, chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS chats (
    chat_type TEXT NOT NULL,
    chat_id   TEXT NOT NULL,
    name      TEXT,
    PRIMARY KEY (chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(text, content='messages', content_rowid='rowid', tokenize='unicode61');
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def insert_messages(con: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur = con.executemany(
        """INSERT INTO messages(chat_type, chat_id, msg_id, ts, sender_qq, sender_uid, direction, kind, text)
           VALUES(:chat_type, :chat_id, :msg_id, :ts, :sender_qq, :sender_uid, :direction, :kind, :text)
           ON CONFLICT(chat_type, msg_id) DO UPDATE SET
             text = excluded.text, kind = excluded.kind, ts = excluded.ts""",
        rows,
    )
    return cur.rowcount


def rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")


def set_chat_name(con: sqlite3.Connection, chat_type: str, chat_id: str, name: str) -> None:
    con.execute(
        """INSERT INTO chats(chat_type, chat_id, name) VALUES(?, ?, ?)
           ON CONFLICT(chat_type, chat_id) DO UPDATE SET name = excluded.name""",
        (chat_type, chat_id, name),
    )


def chat_names(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (r["chat_type"], r["chat_id"]): r["name"]
        for r in con.execute("SELECT chat_type, chat_id, name FROM chats WHERE name IS NOT NULL")
    }


def list_chats(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        con.execute(
            """SELECT m.chat_type, m.chat_id, c.name,
                      COUNT(*) AS n, MAX(m.ts) AS last_ts
               FROM messages m
               LEFT JOIN chats c ON c.chat_type = m.chat_type AND c.chat_id = m.chat_id
               GROUP BY m.chat_type, m.chat_id
               ORDER BY last_ts DESC"""
        )
    )


def resolve_chat(con: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """Resolve a chat by exact id, name substring, or id suffix."""
    rows = list(
        con.execute(
            """SELECT m.chat_type, m.chat_id, c.name, COUNT(*) AS n, MAX(m.ts) AS last_ts
               FROM messages m
               LEFT JOIN chats c ON c.chat_type = m.chat_type AND c.chat_id = m.chat_id
               WHERE m.chat_id = ? OR c.name LIKE ?
               GROUP BY m.chat_type, m.chat_id
               ORDER BY last_ts DESC""",
            (needle, f"%{needle}%"),
        )
    )
    return rows


def fetch_messages(
    con: sqlite3.Connection,
    chat_type: str | None = None,
    chat_id: str | None = None,
    *,
    limit: int | None = None,
    after: int | None = None,
    before: int | None = None,
    ascending: bool = True,
) -> list[sqlite3.Row]:
    where, args = [], []
    if chat_type:
        where.append("chat_type = ?")
        args.append(chat_type)
    if chat_id:
        where.append("chat_id = ?")
        args.append(chat_id)
    if after:
        where.append("ts >= ?")
        args.append(after)
    if before:
        where.append("ts <= ?")
        args.append(before)
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts " + ("ASC" if ascending else "DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(con.execute(sql, args))
    if not ascending and limit:
        rows.reverse()
    return rows


def search(
    con: sqlite3.Connection,
    query: str,
    chat_type: str | None = None,
    chat_id: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    where, args = ["messages_fts MATCH ?"], [query]
    if chat_type:
        where.append("m.chat_type = ?")
        args.append(chat_type)
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    args.append(limit)
    return list(
        con.execute(
            f"""SELECT m.*, snippet(messages_fts, 0, '[', ']', '…', 12) AS snip
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                WHERE {' AND '.join(where)}
                ORDER BY m.ts DESC LIMIT ?""",
            args,
        )
    )


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def to_json(row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "chat_type": row["chat_type"],
            "chat_id": row["chat_id"],
            "msg_id": row["msg_id"],
            "ts": row["ts"],
            "time": fmt_ts(row["ts"]),
            "sender": row["sender_qq"] or row["sender_uid"],
            "direction": row["direction"],
            "kind": row["kind"],
            "text": row["text"],
        },
        ensure_ascii=False,
    )
