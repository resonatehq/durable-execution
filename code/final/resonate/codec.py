"""A document as JSON, and the key it lives under."""

from __future__ import annotations

from pydantic import TypeAdapter

from .kernel import Document

DOCUMENT = TypeAdapter(Document)


def doc_key(origin: str, prefix: str = "") -> str:
    """Where an origin's document lives. The origin is percent-encoded: `/`
    would create a path segment and `:` is the origin separator this design
    reserves, so both are escaped along with everything non-alphanumeric."""
    escaped = []
    for b in origin.encode("utf-8"):
        c = chr(b)
        escaped.append(c if (c.isalnum() and b < 128) or c in ".-" else f"%{b:02X}")
    return f"{prefix}wf/{''.join(escaped)}"


def encode(doc: Document) -> bytes:
    return DOCUMENT.dump_json(doc)


def decode(raw: bytes) -> Document:
    return DOCUMENT.validate_json(raw)
