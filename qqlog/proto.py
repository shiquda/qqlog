"""Minimal protobuf reader for NTQQ message bodies (column 40800)."""

from __future__ import annotations


class ProtoError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def iter_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one message level."""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = read_varint(buf, pos)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise ProtoError("field 0")
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 1:
            value = buf[pos : pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                raise ProtoError("truncated length-delimited field")
            value = buf[pos : pos + length]
            pos += length
        elif wire == 5:
            value = buf[pos : pos + 4]
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        yield field, wire, value


def _walk(buf: bytes, depth: int = 0):
    """Yield (field, wire, value) for every nested level (breadth-first is not needed)."""
    for field, wire, value in iter_fields(buf):
        yield field, wire, value
        if wire == 2 and depth < 4 and len(value) >= 2:
            try:
                sub = list(iter_fields(value))
            except ProtoError:
                continue
            if not sub:
                continue
            for item in _walk(value, depth + 1):
                yield item


TEXT_FIELD = 45101
CTYPE_FIELD = 45002


def parse_body(blob: bytes) -> dict:
    """Parse a 40800 MsgBody blob into text segments and content hints.

    NTQQ stores each message as `MsgBody -> repeated MsgContent`; every MsgContent
    carries its own element type (45002) and, for text, the payload in 45101.
    Iteration is defensive: unknown or broken elements are skipped rather than
    aborting the whole message.
    """
    texts: list[str] = []
    ctypes: list[int] = []
    md5s: list[str] = []
    filenames: list[str] = []

    for field, wire, value in iter_fields(blob):
        if wire != 2:
            continue
        text = None
        ctype = None
        try:
            sub_fields = list(iter_fields(value))
        except ProtoError:
            continue
        for sf, sw, sv in sub_fields:
            if sf == TEXT_FIELD and sw == 2:
                try:
                    text = sv.decode("utf-8")
                except UnicodeDecodeError:
                    text = sv.decode("utf-8", "replace")
            elif sf == CTYPE_FIELD and sw == 0:
                ctype = sv
        if text is not None:
            texts.append(text)
        if ctype is not None:
            ctypes.append(ctype)
        if any(sf == 45406 for sf, _, _ in sub_fields):
            for sf, sw, sv in sub_fields:
                if sf == 45406 and sw == 2:
                    md5s.append(sv.hex().upper() if len(sv) == 16 else sv.decode("ascii", "replace").upper())
                elif sf == 45405 and sw == 2:
                    filenames.append(sv.decode("utf-8", "replace"))

    return {
        "text": "\n".join(texts),
        "ctypes": ctypes,
        "md5s": md5s,
        "filenames": filenames,
    }


def classify(body: dict) -> str:
    """Best-effort message kind from parsed content hints."""
    if body["text"].strip():
        return "text"
    if body["md5s"]:
        return "image"
    if body["filenames"]:
        return "file"
    ctypes = body["ctypes"]
    if ctypes:
        return f"element:{ctypes[0]}"
    return "other"
