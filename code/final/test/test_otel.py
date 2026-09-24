"""The trace, and the four things that make it one rather than several.

`otel.py` claims that a distributed trace can be assembled with nothing
propagated between the machines that build it. That is an unusual claim --
every tracing library in existence threads a header -- so it is the claim
this file spends most of its assertions on.

The rest is about what the two layers are worth. A logical span alone
cannot tell waiting from working. A physical span alone disappears when a
promise settles without anyone running it, which is what a timer is. Both
together answer the question a person actually asks, and each of those is
a test below.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import otel
from engine import Engine
from kernel import KernelCfg
from ports import Unavailable
from queue_mem import Queue
from runtime import Clock, Runtime, Worker
from sdk import gather, resonate
from store_mem import Store

CFG = KernelCfg(retry_timeout=30_000)


@resonate
async def leaf(x: str):
    return f"got {x}"


@resonate
async def branch(x: str):
    return await gather(leaf.rpc(f"{x}-a"), leaf.rpc(f"{x}-b"))


#: How many times `flaky` has been entered, across the whole run. A module
#: global rather than an argument, because the point is that the *same*
#: durable call is attempted more than once.
ATTEMPTS: list[str] = []


@resonate
async def flaky(x: str):
    """Fails the way a bucket fails: twice, and then not."""
    ATTEMPTS.append(x)
    if len(ATTEMPTS) < 3:
        raise Unavailable("the bucket said 503")
    return f"got {x} on attempt {len(ATTEMPTS)}"


def world(*functions):
    store, queue, clock = Store(), Queue(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve("worker://w", Worker(engine, clock, "w-1"), *functions)
    return rt, store, clock


def run(origin: str, fn, *args, functions=()):
    rt, store, clock = world(*(functions or (fn,)))
    with otel.collecting() as spans:
        rt.start(origin, fn, *args)
        rt.drain()
    return spans, rt, store, clock


def logical(spans):
    return [s for s in spans if s.attributes["de.span"] == "logical"]


def physical(spans):
    return [s for s in spans if s.attributes["de.span"] == "physical"]


# --- nothing is propagated -------------------------------------------------


def test_the_ids_survive_a_process_boundary():
    """The whole claim, tested the only way that means anything.

    `hash()` is salted per process, so a span id built with it would put
    the two halves of one run into two traces -- and every single-process
    test would still pass. This runs the derivation in a fresh interpreter
    with a hash seed that cannot match ours.
    """
    src = ("import sys; sys.path.insert(0, '.'); import otel; "
           "print(otel.trace_id('research.1').hex(), otel.span_id('research.1:2').hex())")
    out = subprocess.run([sys.executable, "-c", src], capture_output=True,
                         text=True, env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin"})
    assert out.returncode == 0, out.stderr
    there = out.stdout.split()
    here = [otel.trace_id("research.1").hex(), otel.span_id("research.1:2").hex()]
    assert there == here, (
        f"another process derives {there}, we derive {here}: a run resumed on "
        "a second container would land in a trace of its own")


def test_one_run_is_one_trace():
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    assert len({s.trace for s in spans}) == 1, "one run, more than one trace"
    assert {s.trace for s in spans} == {otel.trace_id("branch.1")}


def test_two_runs_are_two_traces():
    a, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    b, *_ = run("branch.2", branch, "q", functions=(branch, leaf))
    assert a[0].trace != b[0].trace


def test_the_tree_is_connected():
    """Every span has a parent that exists, or is the root. An id derived
    wrongly does not error -- it produces an orphan, which a viewer draws as
    a second root and a person reads as two unrelated things."""
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    ids = {s.span for s in spans}
    orphans = [s for s in spans if s.parent is not None and s.parent not in ids]
    assert not orphans, f"orphaned: {orphans}"
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1 and roots[0].span == otel.span_id("branch.1")


@pytest.mark.parametrize("id,parent", [
    ("o", None), ("o.1", None), ("o.1:2", "o.1"), ("o.1:2.3", "o.1:2"),
    ("o.1:2.3.4", "o.1:2.3"),
])
def test_the_parent_is_the_position_above(id, parent):
    assert otel.dewey_parent(id) == parent


def test_no_two_spans_share_an_id():
    """The one way a derived id can be wrong without being orphaned. A root
    that suspends and resumes runs its body more than once, and if the two
    attempts collided the trace would silently lose one."""
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    ids = [s.span for s in spans]
    assert len(set(ids)) == len(ids), "two spans with one id"
    assert len(physical(spans)) > len({s.attributes["de.promise"]
                                       for s in physical(spans)}), \
        "no promise was attempted twice, so this proved nothing -- pick a run that suspends"


# --- what the two layers are for -------------------------------------------


def test_every_logical_span_that_ran_has_physical_ones():
    """Uniform, not conditional. If attempts were spans only when something
    went wrong, the shape of the trace would depend on the outcome and you
    could not tell a fast run from an uninstrumented one."""
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    ran = {s.attributes["de.promise"] for s in physical(spans)}
    assert {s.attributes["de.promise"] for s in logical(spans)} == ran


def test_a_promise_settles_once_and_gets_one_span():
    """A run commits far more often than it settles. The logical span is
    emitted by whoever commits the settlement, and a settlement is committed
    once, so this holds by construction rather than by deduplication."""
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    ids = [s.attributes["de.promise"] for s in logical(spans)]
    assert sorted(ids) == sorted(set(ids)), f"settled twice: {ids}"
    assert set(ids) == {"branch.1", "branch.1:1", "branch.1:2"}


def test_the_logical_span_is_the_promise_not_the_process():
    """Its ends come from the document, which is why a second process would
    emit the same span rather than a competing version of it."""
    spans, rt, store, _ = run("branch.1", branch, "q", functions=(branch, leaf))
    from codec import decode, doc_key
    doc = decode(store.get(doc_key("branch.1"))[0].encode(), "branch.1")
    for s in logical(spans):
        p = doc.get(s.attributes["de.promise"]).promise
        assert (s.start_ms, s.end_ms) == (p.created_at, p.settled_at)


def test_waiting_is_the_difference_between_the_layers():
    """The question neither layer answers alone, and the reason for both."""
    from demo import nap
    rt, store, clock = world(nap)
    with otel.collecting() as spans:
        rt.start("nap.1", nap, 5_000)
        rt.drain()
        clock.advance(5_000)
        rt.drain()

    root = next(s for s in logical(spans) if s.attributes["de.promise"] == "nap.1")
    worked = sum(s.duration_ms for s in physical(spans)
                 if s.attributes["de.promise"] == "nap.1")
    assert root.duration_ms == 5_000
    assert worked == 0, worked
    assert root.duration_ms - worked == 5_000, (
        "the run spent five seconds not working, and only the pair says so")


def test_a_timer_has_no_attempt_because_nothing_runs_it():
    """Time settles it. There is no process to have a physical span."""
    from demo import nap
    rt, store, clock = world(nap)
    with otel.collecting() as spans:
        rt.start("nap.1", nap, 5_000)
        rt.drain()
        clock.advance(5_000)
        rt.drain()
    timer = [s for s in logical(spans) if s.name.startswith("sleep")]
    assert len(timer) == 1 and timer[0].status == otel.OK
    assert not [s for s in physical(spans)
                if s.attributes["de.promise"] == timer[0].attributes["de.promise"]]


def test_a_suspension_is_not_an_error():
    """Stopping to wait for a value you do not have is how this system makes
    progress. Calling it an error would paint every fan-out red."""
    spans, *_ = run("branch.1", branch, "q", functions=(branch, leaf))
    waited = [s for s in physical(spans) if s.attributes.get("de.outcome") == "suspended"]
    assert waited, "nothing suspended, so this proved nothing"
    assert all(s.status == otel.UNSET for s in waited), [s.status for s in waited]
    assert all("de.error" not in s.attributes for s in waited)


def test_an_attempt_that_records_nothing_is_still_a_span():
    """The case the document cannot show, and the reason this is live.

    A platform failure is not the function's answer: nothing settles, the
    task goes back at the same version, and somebody tries again. When one
    of the tries finally works, the document says `resolved` and says it in
    exactly the words it would have used had the first try worked. The two
    failures are nowhere in it -- not as a field, not as a state, not as an
    extra object. They happened, and the physical spans are the only place
    they exist.

    Both of the day's real deployment bugs looked like this. A trace built
    by reading documents back would have shown neither.
    """
    ATTEMPTS.clear()
    spans, rt, store, _ = run("flaky.1", flaky, "q")
    assert len(ATTEMPTS) == 3, ATTEMPTS

    settled = [s for s in logical(spans) if s.attributes["de.promise"] == "flaky.1"]
    assert len(settled) == 1 and settled[0].status == otel.OK, settled

    from codec import decode, doc_key
    doc = decode(store.get(doc_key("flaky.1"))[0].encode(), "flaky.1")
    assert doc.get("flaky.1").promise.state == "resolved"
    assert "503" not in store.get(doc_key("flaky.1"))[0], \
        "the document remembers the failures, so this test is about the wrong thing"

    tried = [s for s in physical(spans) if s.attributes["de.promise"] == "flaky.1"]
    failed = [s for s in tried if s.status == otel.ERROR]
    assert len(failed) == 2, [s.attributes.get("de.outcome") for s in tried]
    assert {s.attributes["de.error"] for s in failed} == {"Unavailable"}
    assert [s for s in tried if s.status == otel.OK], "the try that worked left no span"


# --- off by default --------------------------------------------------------


def test_nothing_is_emitted_when_nobody_is_listening():
    """Off is the default, and off means nothing is built rather than built
    and dropped. The same run, twice, with the sink the only difference."""
    seen: list = []
    try:
        otel.to(seen.append)
        rt, *_ = world(branch, leaf)
        rt.start("branch.1", branch, "q")
        rt.drain()
        assert seen, "the process-wide sink was never reached"
        was = len(seen)
    finally:
        otel.to(None)

    assert otel.sink() is None
    rt, *_ = world(branch, leaf)
    rt.start("branch.2", branch, "q")
    rt.drain()
    assert len(seen) == was, "spans were emitted with the sink turned off"


def test_collecting_is_this_context_only():
    with otel.collecting() as outer:
        with otel.collecting() as inner:
            otel.emit(otel.Span("x", b"\x01" * 16, b"\x02" * 8, None, 0, 1))
        otel.emit(otel.Span("y", b"\x01" * 16, b"\x03" * 8, None, 0, 1))
    assert [s.name for s in inner] == ["x"]
    assert [s.name for s in outer] == ["y"]
    assert otel.sink() is None
