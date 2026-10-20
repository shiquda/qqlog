"""Copy messages out of the decrypted nt_msg.db into the qqlog index."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import proto, store

TABLES = {
    "c2c_msg_table": "c2c",
    "group_msg_table": "group",
}

# NTQQ stores message fields as the original protobuf field numbers.
FIELD_MSG_ID = "40001"
FIELD_TS = "40050"
FIELD_BODY = "40800"
FIELD_SENDER_UID = "40020"
FIELD_SENDER_QQ = "40033"
FIELD_PEER_UID = "40021"
FIELD_PEER_QQ = "40030"
FIELD_DIRECTION = "40013"

MIN_COLUMNS = (FIELD_MSG_ID, FIELD_TS, FIELD_BODY)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _select_list(cols: set[str]) -> str:
    def col(name: str, alias: str) -> str:
        return f'"{name}" AS {alias}' if name in cols else f"NULL AS {alias}"

    parts = [
        col(FIELD_MSG_ID, "msg_id"),
        col(FIELD_TS, "ts"),
        col(FIELD_BODY, "body"),
        col(FIELD_SENDER_UID, "sender_uid"),
        col(FIELD_SENDER_QQ, "sender_qq"),
        col(FIELD_PEER_UID, "peer_uid"),
        col(FIELD_PEER_QQ, "peer_qq"),
        col(FIELD_DIRECTION, "direction"),
    ]
    return ", ".join(parts)


def _chat_id(row: sqlite3.Row, chat_type: str) -> str:
    peer_qq = row["peer_qq"]
    peer_uid = row["peer_uid"]
    if peer_qq not in (None, "", 0, "0"):
        return str(peer_qq)
    if peer_uid:
        return str(peer_uid)
    return "unknown"

def _load_names(data_dir: Path) -> tuple[dict[str, str], dict[str, str], str | None, str | None]:
    """返回 (group_names, friend_names, my_uin, my_uid)"""
    gnames, fnames = {}, {}
    my_uin, my_uid = None, None
    gp = data_dir / "group_info.db.plain"
    if gp.exists():
        try:
            c = sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
            for gid, name in c.execute('SELECT "60001", "60007" FROM group_detail_info_ver1 WHERE "60007" IS NOT NULL'):
                if gid and name:
                    gnames[str(gid)] = name
            c.close()
        except Exception:
            pass
    pp = data_dir / "profile_info.db.plain"
    if pp.exists():
        try:
            c = sqlite3.connect(f"file:{pp}?mode=ro", uri=True)
            for qq, uid, nick in c.execute('SELECT "1002", "1000", "20002" FROM profile_info_v6 WHERE "20002" IS NOT NULL AND "20002" != ""'):
                if qq and nick:
                    fnames[str(qq)] = nick
                if uid and nick:
                    fnames[str(uid)] = nick
            # 本机账号通常在 profile_info_v6 的第一行或有特殊标记
            first = c.execute('SELECT "1002", "1000" FROM profile_info_v6 LIMIT 1').fetchone()
            if first:
                my_uin = str(first[0]) if first[0] else None
                my_uid = str(first[1]) if first[1] else None
            c.close()
        except Exception:
            pass
    return gnames, fnames, my_uin, my_uid


def sync(plain: Path, index: Path, *, force: bool = False, batch: int = 2000) -> dict:
    if not Path(plain).exists():
        raise FileNotFoundError(f"decrypted database not found: {plain}")

    src = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    con = store.connect(index)

    stats = {"inserted": 0, "tables": {}, "seconds": 0.0}
    started = time.time()
    gnames, fnames, my_uin, my_uid = _load_names(plain.parent)
    for gid, name in gnames.items():
        store.set_chat_name(con, "group", gid, name)
    for q_or_u, name in fnames.items():
        store.set_chat_name(con, "c2c", q_or_u, name)
    con.commit()
    try:
        for table, chat_type in TABLES.items():
            cols = _columns(src, table)
            if not cols or not set(MIN_COLUMNS) <= cols:
                stats["tables"][table] = "skipped (missing columns)"
                continue

            since = 0
            if not force:
                raw = store.get_meta(con, f"cursor:{table}")
                if raw:
                    since = max(0, int(raw) - 7200)  # 2h overlap for late edits

            sql = (
                f'SELECT {_select_list(cols)} FROM "{table}"'
                + (f' WHERE "{FIELD_TS}" > ?' if since else "")
                + f' ORDER BY "{FIELD_TS}"'
            )
            args = (since,) if since else ()

            batch_rows: list[dict] = []
            n_table = 0
            max_ts = since
            for row in src.execute(sql, args):
                body = row["body"]
                if not body:
                    continue
                try:
                    parsed = proto.parse_body(body)
                except proto.ProtoError:
                    continue
                batch_rows.append(
                    {
                        "chat_type": chat_type,
                        "chat_id": _chat_id(row, chat_type),
                        "msg_id": str(row["msg_id"]),
                        "ts": int(row["ts"] or 0),
                        "sender_qq": None if row["sender_qq"] in (None, 0) else str(row["sender_qq"]),
                        "sender_uid": row["sender_uid"],
                        "direction": 1 if (row["direction"] == 2 or (my_uin and str(row["sender_qq"]) == my_uin) or (my_uid and row["sender_uid"] == my_uid)) else 0,
                        "kind": proto.classify(parsed),
                        "text": parsed["text"],
                    }
                )
                max_ts = max(max_ts, int(row["ts"] or 0))
                if len(batch_rows) >= batch:
                    stats["inserted"] += store.insert_messages(con, batch_rows)
                    con.commit()
                    n_table += len(batch_rows)
                    batch_rows = []

            stats["inserted"] += store.insert_messages(con, batch_rows)
            con.commit()
            n_table += len(batch_rows)
            if max_ts:
                store.set_meta(con, f"cursor:{table}", str(max_ts))
            stats["tables"][table] = n_table

        store.rebuild_fts(con)
        store.set_meta(con, "synced_at", str(int(time.time())))
        con.commit()
    finally:
        src.close()
        con.close()

    stats["seconds"] = round(time.time() - started, 1)
    return stats
