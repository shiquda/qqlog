"""Decrypt NTQQ nt_msg.db (1024-byte header + SQLCipher 4) into a plain SQLite file."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

HEADER_SIZE = 1024
_KEY_RE = re.compile(r"^[\x20-\x7e]{8,128}$")

PRAGMAS = (
    "PRAGMA cipher_page_size = 4096;",
    "PRAGMA kdf_iter = 4000;",
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;",
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;",
)


class DecryptError(RuntimeError):
    pass


def split_header(src: Path, dst: Path) -> None:
    """Copy src without the 1024-byte NTQQ header."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(HEADER_SIZE)
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            fout.write(chunk)


def open_encrypted(path: Path, key: str):
    try:
        from sqlcipher3 import dbapi2
    except ImportError as exc:  # pragma: no cover
        raise DecryptError(
            "sqlcipher3 is not installed; run: uv sync (or uv pip install sqlcipher3)"
        ) from exc

    if not _KEY_RE.match(key):
        raise DecryptError("key must be 8-128 printable ASCII characters")

    con = dbapi2.connect(str(path))
    cur = con.cursor()
    cur.execute("PRAGMA cipher_page_size = 4096;")
    escaped = key.replace("'", "''")
    cur.execute(f"PRAGMA key = '{escaped}';")
    for pragma in PRAGMAS[1:]:
        cur.execute(pragma)
    try:
        cur.execute("SELECT count(*) FROM sqlite_master;")
        cur.fetchone()
    except Exception as exc:
        con.close()
        raise DecryptError(f"cannot open database (wrong key?): {exc}") from exc
    return con


def decrypt(src: Path, out: Path, key: str, *, force: bool = False) -> Path:
    """Decrypt src into out (a plain SQLite file). Reuses a fresh cache unless forced."""
    src = Path(src)
    out = Path(out)
    if out.exists() and not force and out.stat().st_mtime >= src.stat().st_mtime:
        return out

    tmp_clean = out.with_suffix(".clean.tmp")
    tmp_out = out.with_suffix(".tmp")
    for p in (tmp_clean, tmp_out):
        if p.exists():
            p.unlink()

    started = time.time()
    split_header(src, tmp_clean)
    con = open_encrypted(tmp_clean, key)
    try:
        cur = con.cursor()
        cur.execute(f"ATTACH DATABASE '{tmp_out}' AS plaintext KEY '';")
        cur.execute("SELECT sqlcipher_export('plaintext');")
        cur.execute("DETACH DATABASE plaintext;")
        con.commit()
    finally:
        con.close()
        if tmp_clean.exists():
            os.unlink(tmp_clean)

    tmp_out.replace(out)
    out.touch()
    _ = started  # timing is reported by the caller
    return out
