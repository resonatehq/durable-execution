"""The document's canonical byte form.

One line of JSON per object, a header first, joined by `\\n`. Two encoders
given equal documents produce identical bytes.

## What that is worth, stated honestly

Nothing in this system requires it. The write law compares decoded
substance — the objects and the armed deadline — not bytes, and a writer
that lost the answer to a write recovers by re-deciding rather than by
recognising its own bytes. The only byte comparison anywhere is the
round-trip test in `test_engine.py`, which is the codec checking itself.
An earlier version of this docstring claimed the engine compares bytes. It
does not, and never did.

What canonicity actually buys is optionality, for about four lines of
code: two `sorted()` calls, a fixed key order that you write in some order
anyway, and ASCII output that `json.dumps` gives by default. With it:

- two generations of an object in the bucket can be diffed to see a
  transition, which is the first thing anyone does when something goes
  wrong in production;
- a second implementation can be held to these bytes rather than to a
  decoding of them, which is what the differential test against the Rust
  server would want;
- the schema corpus and the exhaustive explorer produce the same documents
  on every run, so a failure is reproducible rather than merely likely.

That last one is the only place it bites today. `Task.resumes` is a set,
and a set of strings iterates in an order that depends on `PYTHONHASHSEED`,
so without the `sorted()` the same state serialises differently in
different processes. Nothing breaks — but nothing is reproducible either.

## The rules, and what each is for

- **ASCII only**, so the bytes do not depend on anyone's locale or encoder.
- **Fixed key order** within a line and **fixed line order** between them (the
  document is already sorted by Dewey id), so equal state means equal bytes.
- **Map keys sorted**, because a tag map has no order of its own.
- **Omission, never null or empty**, so a field that is absent and a field that
  is empty cannot produce two spellings of one state.
- **No insignificant whitespace**, and integers without exponents.

The header binds the document to the key it lives under. A document whose
`og` does not hash the origin it was read under is refused rather than
silently answering for another workflow.
"""

from __future__ import annotations

import json

from kernel import (
    Document, Object, Promise, Task, Value, dewey,
)

VERSION = 1


class Malformed(Exception):
    """The bytes are not a document this reader can make sense of."""


def origin_hash(origin: str) -> str:
    """16 lowercase hex of 64-bit FNV-1a. Not a security property: it binds a
    document to its key, so a misrouted read is refused."""
    h = 0xCBF29CE484222325
    for b in origin.encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def encode_key(s: str) -> str:
    """Percent-encode everything that is not safe and stable in a key. `/`
    would create a path segment and `:` is the origin separator this design
    reserves, so both are escaped along with everything non-alphanumeric."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        out.append(c if (c.isalnum() and b < 128) or c in ".-" else f"%{b:02X}")
    return "".join(out)


def doc_key(origin: str, prefix: str = "") -> str:
    return f"{prefix}wf/{encode_key(origin)}"


def _line(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def _value(v: Value) -> dict:
    out: dict = {}
    if v.headers:
        out["h"] = dict(sorted(v.headers.items()))
    if v.data is not None:
        out["d"] = v.data
    return out


def encode(doc: Document, origin: str) -> bytes:
    header: dict = {"t": "h", "v": VERSION, "clk": doc.clock, "g": doc.gen,
                    "og": origin_hash(origin)}
    if doc.timer_at is not None:
        header["ta"] = doc.timer_at
    if doc.timer_name is not None:
        header["tn"] = doc.timer_name
    lines = [_line(header)]
    for o in doc.objects:
        p, t = o.promise, o.task
        row: dict = {"t": "o", "id": o.id, "st": p.state}
        if p.tags:
            row["tg"] = dict(sorted(p.tags.items()))
        if _value(p.param):
            row["pm"] = _value(p.param)
        if _value(p.value):
            row["vl"] = _value(p.value)
        row["to"] = p.timeout_at
        row["ca"] = p.created_at
        if p.settled_at is not None:
            row["sa"] = p.settled_at
        if p.callbacks:
            row["cb"] = list(p.callbacks)      # registration order is protocol-visible
        if p.listeners:
            row["ls"] = list(p.listeners)      # and so is this one
        if t is not None:
            task: dict = {"st": t.state, "v": t.version}
            if t.pid is not None:
                task["pid"] = t.pid
            if t.ttl is not None:
                task["ttl"] = t.ttl
            if t.resumes:
                task["rs"] = sorted(t.resumes)  # a set on the way in, so sorted on the way out
            if t.retry_at is not None:
                task["ra"] = t.retry_at
            if t.lease_at is not None:
                task["la"] = t.lease_at
            row["k"] = task
        lines.append(_line(row))
    return "\n".join(lines).encode("ascii")


def _read_value(d: dict | None) -> Value:
    d = d or {}
    return Value(headers=d.get("h"), data=d.get("d"))


def decode(raw: bytes, origin: str) -> Document:
    try:
        rows = [json.loads(line) for line in raw.decode("ascii").split("\n")]
    except (UnicodeDecodeError, ValueError) as e:
        raise Malformed(str(e)) from None
    if not rows or not isinstance(rows[0], dict) or rows[0].get("t") != "h":
        raise Malformed("no header")
    h = rows[0]
    if h.get("v", 0) > VERSION:
        raise Malformed(f"version {h.get('v')} is newer than {VERSION}")
    if h.get("og") != origin_hash(origin):
        raise Malformed(f"document does not belong to origin {origin!r}")

    doc = Document(clock=h.get("clk", 0), gen=h.get("g", 0),
                   timer_at=h.get("ta"), timer_name=h.get("tn"))
    for row in rows[1:]:
        if not isinstance(row, dict) or row.get("t") != "o":
            continue  # a line type this reader does not know: skip it, as the format allows
        p = Promise(
            state=row["st"],
            param=_read_value(row.get("pm")),
            value=_read_value(row.get("vl")),
            tags=dict(row.get("tg", {})),
            timeout_at=row["to"],
            created_at=row["ca"],
            settled_at=row.get("sa"),
            callbacks=list(row.get("cb", [])),
            listeners=list(row.get("ls", [])),
        )
        k = row.get("k")
        t = None if k is None else Task(
            state=k["st"], version=k["v"], pid=k.get("pid"), ttl=k.get("ttl"),
            resumes=set(k.get("rs", [])), retry_at=k.get("ra"), lease_at=k.get("la"),
        )
        doc.objects.append(Object(id=row["id"], promise=p, task=t))
    if [o.id for o in doc.objects] != sorted((o.id for o in doc.objects), key=dewey):
        raise Malformed("objects are not in Dewey order")
    return doc
