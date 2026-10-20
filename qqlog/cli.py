"""qqlog command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, crypto, paths, store, sync as sync_mod


# ── helpers ───────────────────────────────────────────────────────────────────


def _open_index():
    return store.connect(paths.index_db())


def _parse_time(value: str | None) -> int | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"cannot parse time: {value!r} (use YYYY-MM-DD[ HH:MM[:SS]])")


def _chat_arg(con, needle: str):
    rows = store.resolve_chat(con, needle)
    if not rows:
        raise SystemExit(f"no chat matches {needle!r}; run `qqlog chats`")
    if len(rows) > 1:
        lines = "\n".join(
            f"  {r['chat_type']:5s} {r['chat_id']:<14s} {r['name'] or ''} ({r['n']} msgs)"
            for r in rows[:10]
        )
        raise SystemExit(f"{needle!r} matches {len(rows)} chats:\n{lines}")
    return rows[0]


def _require_key() -> str:
    cfg = paths.load_config()
    key = os.environ.get("QQ_KEY") or cfg.get("key")
    if not key:
        raise SystemExit("no key configured; run `qqlog key set <key>` or export QQ_KEY")
    return key


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_key(args) -> int:
    cfg = paths.load_config()
    if args.action == "set":
        key = args.value.strip()
        if not key:
            raise SystemExit("empty key")
        cfg["key"] = key
        p = paths.save_config(cfg)
        print(f"saved key ({len(key)} bytes) to {p}")
        return 0
    key = cfg.get("key") or os.environ.get("QQ_KEY")
    if not key:
        print("no key configured")
        return 1
    print(f"key: {key[:2]}{'*' * (len(key) - 4)}{key[-2:]}  ({len(key)} bytes)")
    return 0


def cmd_status(args) -> int:
    enc = paths.nt_msg_db()
    print(f"config      : {paths.config_path()}")
    print(f"encrypted db: {enc or 'not found'}")
    plain = paths.plain_db()
    print(f"plain db    : {plain} {'(' + str(plain.stat().st_size // 1048576) + ' MB)' if plain.exists() else '(missing)'}")
    idx = paths.index_db()
    print(f"index db    : {idx} {'(' + str(idx.stat().st_size // 1048576) + ' MB)' if idx.exists() else '(missing)'}")
    if idx.exists():
        con = _open_index()
        n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        c = con.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
        print(f"messages    : {n} in {c} chats")
        con.close()
    return 0


def cmd_decrypt(args) -> int:
    src = Path(args.source).expanduser() if args.source else paths.nt_msg_db()
    if not src:
        raise SystemExit("cannot locate nt_msg.db; pass --source")
    key = _require_key()
    out = Path(args.output).expanduser() if args.output else paths.plain_db()
    print(f"decrypting {src} -> {out} ...")
    crypto.decrypt(Path(src), out, key, force=args.force)
    print(f"ok: {out} ({out.stat().st_size // 1048576} MB)")
    # 尝试解密同目录下的 group_info.db 和 profile_info.db 以支持群名与好友昵称
    for extra in ("group_info.db", "profile_info.db"):
        ep = src.parent / extra
        if ep.exists():
            eout = paths.data_dir() / f"{extra}.plain"
            try:
                crypto.decrypt(ep, eout, key, force=args.force)
                print(f"ok: {eout.name} ({eout.stat().st_size // 1024} KB)")
            except Exception as e:
                print(f"warning: decrypt {extra}: {e}", file=sys.stderr)
    return 0


def cmd_sync(args) -> int:
    plain = Path(args.plain).expanduser() if args.plain else paths.plain_db()
    if not plain.exists():
        raise SystemExit(f"{plain} is missing; run `qqlog decrypt` first")
    stats = sync_mod.sync(plain, paths.index_db(), force=args.force)
    for table, n in stats["tables"].items():
        print(f"{table:18s} {n}")
    print(f"inserted/updated {stats['inserted']} messages in {stats['seconds']}s")
    return 0


def cmd_chats(args) -> int:
    con = _open_index()
    rows = store.list_chats(con)
    con.close()
    if args.json:
        for r in rows:
            print(json.dumps(dict(r), ensure_ascii=False))
        return 0
    for r in rows[: args.limit or len(rows)]:
        name = r["name"] or ""
        print(
            f"{r['chat_type']:5s} {r['chat_id']:<12s} {name[:24]:<24s} "
            f"{r['n']:>7d}  {store.fmt_ts(r['last_ts'])}"
        )
    return 0


def cmd_list(args) -> int:
    con = _open_index()
    chat = _chat_arg(con, args.chat)
    rows = store.fetch_messages(
        con, chat["chat_type"], chat["chat_id"], limit=args.limit, ascending=False
    )
    con.close()
    for r in rows:
        who = "me" if r["direction"] == 1 else (r["sender_qq"] or r["sender_uid"] or "?")
        print(f"[{store.fmt_ts(r['ts'])}] {who:>10s}  {r['text']}")
    return 0


def cmd_show(args) -> int:
    con = _open_index()
    chat = _chat_arg(con, args.chat)
    rows = store.fetch_messages(
        con,
        chat["chat_type"],
        chat["chat_id"],
        limit=args.limit,
        after=_parse_time(args.after),
        before=_parse_time(args.before),
    )
    con.close()
    for r in rows:
        who = "me" if r["direction"] == 1 else (r["sender_qq"] or r["sender_uid"] or "?")
        print(f"[{store.fmt_ts(r['ts'])}] {who}: {r['text']}")
    return 0


def cmd_search(args) -> int:
    con = _open_index()
    chat_type = chat_id = None
    if args.chat:
        chat = _chat_arg(con, args.chat)
        chat_type, chat_id = chat["chat_type"], chat["chat_id"]
    rows = store.search(con, args.query, chat_type, chat_id, args.limit)
    con.close()
    for r in rows:
        print(f"[{store.fmt_ts(r['ts'])}] {r['chat_type'][:1]}:{r['chat_id']:<12s} {r['snip']}")
    if not rows:
        print("no matches", file=sys.stderr)
        return 1
    return 0


def _export_markdown(rows, title: str) -> str:
    out = [f"# {title}", ""]
    day = None
    for r in rows:
        d = store.fmt_ts(r["ts"])[:10]
        if d != day:
            out.append(f"\n## {d}\n")
            day = d
        who = "我" if r["direction"] == 1 else (r["sender_qq"] or r["sender_uid"] or "?")
        stamp = store.fmt_ts(r["ts"])[11:16]
        text = r["text"].replace("\n", "\n  ")
        out.append(f"- `{stamp}` **{who}**: {text}")
    return "\n".join(out) + "\n"


def _export_html(rows, title: str) -> str:
    import html as h

    body = [f"<!doctype html><meta charset='utf-8'><title>{h.escape(title)}</title>",
            "<style>body{font:14px/1.6 -apple-system,sans-serif;max-width:820px;margin:32px auto;"
            "padding:0 16px;color:#222}h2{font-size:13px;color:#888;margin:24px 0 8px}"
            ".m{margin:6px 0}.t{color:#999;font-size:12px;margin-right:8px}"
            ".me{color:#0a7c3f}.txt{white-space:pre-wrap}</style>",
            f"<h1>{h.escape(title)}</h1>"]
    day = None
    for r in rows:
        d = store.fmt_ts(r["ts"])[:10]
        if d != day:
            body.append(f"<h2>{d}</h2>")
            day = d
        who = "我" if r["direction"] == 1 else (r["sender_qq"] or r["sender_uid"] or "?")
        cls = "me" if r["direction"] == 1 else ""
        body.append(
            f"<div class='m'><span class='t'>{store.fmt_ts(r['ts'])[11:16]}</span>"
            f"<b class='{cls}'>{h.escape(who)}</b><div class='txt'>{h.escape(r['text'])}</div></div>"
        )
    return "".join(body)


def cmd_export(args) -> int:
    con = _open_index()
    if args.all:
        chats = [(r["chat_type"], r["chat_id"], r["name"]) for r in store.list_chats(con)]
    else:
        c = _chat_arg(con, args.chat)
        chats = [(c["chat_type"], c["chat_id"], c["name"])]

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    fmt = args.format
    written = 0
    for chat_type, chat_id, name in chats:
        rows = store.fetch_messages(
            con, chat_type, chat_id,
            after=_parse_time(args.after), before=_parse_time(args.before),
        )
        if not rows:
            continue
        label = name or f"{chat_type}_{chat_id}"
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) if name else ""
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(chat_id))
        tag = f"{safe_name}_{safe_id}" if safe_name else safe_id
        stem = f"{chat_type}_{tag}"[:80] if args.all else "messages"
        if fmt == "jsonl":
            dest = out / f"{stem}.jsonl"
            dest.write_text("\n".join(store.to_json(r) for r in rows) + "\n")
        elif fmt == "md":
            dest = out / f"{stem}.md"
            dest.write_text(_export_markdown(rows, label))
        else:
            dest = out / f"{stem}.html"
            dest.write_text(_export_html(rows, label))
        written += 1
        print(f"{dest}  ({len(rows)} messages)")
    con.close()
    if not written:
        print("nothing to export", file=sys.stderr)
        return 1
    return 0


# ── parser ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qqlog", description="Local QQ NT chat history export & query")
    p.add_argument("--version", action="version", version=f"qqlog {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("key", help="manage the database key")
    k.add_argument("action", choices=["set", "show"])
    k.add_argument("value", nargs="?")
    k.set_defaults(func=cmd_key)

    s = sub.add_parser("status", help="show paths and index stats")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("decrypt", help="decrypt nt_msg.db into plain SQLite")
    d.add_argument("--source")
    d.add_argument("--output")
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_decrypt)

    sy = sub.add_parser("sync", help="index new messages")
    sy.add_argument("--plain")
    sy.add_argument("--force", action="store_true")
    sy.set_defaults(func=cmd_sync)

    c = sub.add_parser("chats", help="list conversations")
    c.add_argument("--limit", type=int)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_chats)

    ls = sub.add_parser("list", help="recent messages in a chat")
    ls.add_argument("chat")
    ls.add_argument("-n", "--limit", type=int, default=30)
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="messages in a chat between two dates")
    sh.add_argument("chat")
    sh.add_argument("--after")
    sh.add_argument("--before")
    sh.add_argument("-n", "--limit", type=int)
    sh.set_defaults(func=cmd_show)

    se = sub.add_parser("search", help="full-text search")
    se.add_argument("query")
    se.add_argument("-c", "--chat")
    se.add_argument("-n", "--limit", type=int, default=50)
    se.set_defaults(func=cmd_search)

    ex = sub.add_parser("export", help="export chats to md/jsonl/html")
    ex.add_argument("chat", nargs="?")
    ex.add_argument("--all", action="store_true")
    ex.add_argument("--format", choices=["md", "jsonl", "html"], default="md")
    ex.add_argument("--out", default="./qqlog-export")
    ex.add_argument("--after")
    ex.add_argument("--before")
    ex.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
