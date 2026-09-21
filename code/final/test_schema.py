"""The line schema, against every document the kernel can reach.

The schema is the one artifact here that is not derived from the code it
grades. That is its whole value, so two things have to be true of it and
both are tested: it must accept everything a correct encoder writes, and it
must reject the ways an encoder goes wrong. A schema that only ever passes
is indistinguishable from no schema.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from codec import encode
from explore import BROAD, NARROW, explore
from kernel import Document, Object, Promise, Task, Value

SCHEMA = json.loads((Path(__file__).parent / "line.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def errors(line: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '(line)'}: {e.message}"
            for e in VALIDATOR.iter_errors(line)]


def lines(doc: Document, origin: str = "o") -> list[dict]:
    return [json.loads(line) for line in encode(doc, origin).decode().split("\n")]


# --- the schema itself -----------------------------------------------------


def test_the_schema_is_a_valid_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


# --- it accepts what a correct encoder writes ------------------------------


@pytest.mark.parametrize("alphabet,depth", [(BROAD, 3), (NARROW, 4)])
def test_every_reachable_document_encodes_to_lines_the_schema_accepts(alphabet, depth):
    """Not a sample. Every state the kernel can reach within the bound, on
    both alphabets, because the shapes an encoder gets wrong are the rare
    ones: a promise born dead, a task with no lease, an empty payload that
    should have been omitted.

    The explorer yields kernel states, and a kernel state is not yet a
    document in a bucket: the name of the armed deadline is the shell's
    field and the kernel never sets it. It is supplied here as the engine
    would, which is what makes these bytes the bytes a bucket would hold.
    The shell's own header is graded separately, on real engine output, by
    the test below.
    """
    bad: list[str] = []
    seen = 0

    def visit(now, doc):
        nonlocal seen
        seen += 1
        named = replace(doc, timer_name=None if doc.timer_at is None else "timer-1")
        for line in lines(named):
            for e in errors(line):
                bad.append(f"{e}  in  {json.dumps(line)[:120]}")

    explore(depth, ab=alphabet, visit=visit)
    assert seen > 1_000, f"only {seen} documents: the corpus is too thin to mean anything"
    assert not bad, bad[:5]


def test_the_engine_writes_lines_the_schema_accepts():
    """The kernel's documents pass above. This is the shell's header, which
    the kernel never touches: the generation, the armed deadline and the name
    the shell gave it."""
    import spec
    from codec import decode, doc_key
    from engine import Engine
    from ports import MemoryTimers, MemoryTransport
    from store_mem import Store

    store = Store()
    e = Engine(store, MemoryTimers(), MemoryTransport(), spec.CFG)
    bad = []
    for msg, now in spec.STANDARD_SCRIPT:
        e.process(msg, now)
        raw = store.get(doc_key(spec.ORIGIN))[0]
        for line in json.loads("[" + raw.replace("}\n{", "},{") + "]"):
            bad += errors(line)
    assert not bad, bad


# --- it rejects the ways an encoder goes wrong -----------------------------

HEADER = {"t": "h", "v": 1, "clk": 10, "g": 2, "og": "af63e24c8601f6be"}
OBJECT = {"t": "o", "id": "o:a", "st": "pending", "tg": {"resonate:target": "http://w"},
          "to": 100, "ca": 1, "k": {"st": "pending", "v": 0, "ra": 50}}


def test_the_reference_lines_are_themselves_accepted():
    assert errors(HEADER) == [] and errors(OBJECT) == []


def mutate(base: dict, **changes) -> dict:
    out = copy.deepcopy(base)
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


#: One per way the bytes can be wrong. Each is a real mistake an encoder
#: makes: a field added, a field dropped, a name misspelled, a domain
#: widened, an omission forgotten.
REJECTED = {
    "an unknown line type": {"t": "z"},
    "a field nobody declared": {"surprise": 1},
    "a misspelled field": {"clock": 10},
    "a format version from the future": {"v": 2},
    "an origin hash that is not one": {"og": "not-a-hash"},
    "an origin hash in upper case": {"og": "AF63E24C8601F6BE"},
    "a clock before the epoch": {"clk": -1},
    "a deadline with no name": {"ta": 500},
    "a name with no deadline": {"tn": "timer-1"},
    "a missing header field": {"og": None},
}

REJECTED_OBJECT = {
    "a promise state nobody defined": {"st": "resolvd"},
    "an empty id": {"id": ""},
    "an empty tag map that should have been omitted": {"tg": {}},
    "an empty payload that should have been omitted": {"pm": {}},
    "a payload with a field nobody declared": {"pm": {"data": "x"}},
    "a tag whose value is not a string": {"tg": {"resonate:target": 7}},
    "an empty callback list that should have been omitted": {"cb": []},
    "a callback registered twice": {"cb": ["o:b", "o:b"]},
    "a listener that is not an address": {"ls": ["worker-1"]},
    "a deadline before the epoch": {"to": -1},
    "a missing deadline": {"to": None},
    "a task state nobody defined": {"k": {"st": "running", "v": 0}},
    "a task with no version": {"k": {"st": "pending"}},
    "a task with a negative version": {"k": {"st": "pending", "v": -1}},
    "a lease of zero, which expires as it is granted": {"k": {"st": "acquired", "v": 1, "pid": "p", "ttl": 0, "la": 9}},
    "a holder with no lease length": {"k": {"st": "acquired", "v": 1, "pid": "p", "la": 9}},
    "a lease length with no holder": {"k": {"st": "acquired", "v": 1, "ttl": 5, "la": 9}},
    "a task waiting to be offered and waiting to die at once": {"k": {"st": "pending", "v": 0, "ra": 1, "la": 2}},
    "a resume recorded twice": {"k": {"st": "pending", "v": 0, "ra": 1, "rs": ["o:x", "o:x"]}},
}


@pytest.mark.parametrize("what,change", sorted(REJECTED.items()))
def test_the_schema_rejects_a_bad_header(what, change):
    assert errors(mutate(HEADER, **change)), what


@pytest.mark.parametrize("what,change", sorted(REJECTED_OBJECT.items()))
def test_the_schema_rejects_a_bad_object(what, change):
    assert errors(mutate(OBJECT, **change)), what


def test_a_line_is_a_header_or_an_object_and_not_both():
    assert errors({**HEADER, **OBJECT}), "a line that claims to be two things"
