"""Filesystem locations used by qqlog."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

APP_NAME = "qqlog"


def data_dir() -> Path:
    base = os.environ.get("QQLOG_HOME")
    if base:
        p = Path(base)
    else:
        p = Path.home() / "Library" / "Application Support" / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> Path:
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.chmod(p, 0o600)
    return p


def discover_nt_db_dirs() -> list[Path]:
    """Find nt_qq_* data dirs for both the sandboxed and unsandboxed layouts."""
    patterns = [
        "~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/nt_qq_*",
        "~/Library/Application Support/QQ/nt_qq_*",
    ]
    seen: list[Path] = []
    for pat in patterns:
        for hit in sorted(glob.glob(os.path.expanduser(pat))):
            p = Path(hit)
            if (p / "nt_db" / "nt_msg.db").exists() and p not in seen:
                seen.append(p)
    return seen


def nt_msg_db() -> Path | None:
    cfg = load_config()
    if cfg.get("encrypted_db"):
        p = Path(os.path.expanduser(cfg["encrypted_db"]))
        if p.exists():
            return p
    for d in discover_nt_db_dirs():
        p = d / "nt_db" / "nt_msg.db"
        if p.exists():
            return p
    return None


def plain_db() -> Path:
    cfg = load_config()
    if cfg.get("plain_db"):
        return Path(os.path.expanduser(cfg["plain_db"]))
    return data_dir() / "nt_msg_plain.db"


def index_db() -> Path:
    cfg = load_config()
    if cfg.get("index_db"):
        return Path(os.path.expanduser(cfg["index_db"]))
    return data_dir() / "qqlog.db"
