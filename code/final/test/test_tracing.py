"""The path a run takes through the system, written down and held to.

`research.trace` is what one run of the research agent does: every
transition, every read, every write, every task, nested by who called
whom. It is in the repository because somebody read it and agreed that is
the path — that the fan-out dispatches three searches before it blocks,
that the replay reads five promises back and pays for none of them, that
the deadline moves when the lease is shorter than the retry timeout.

So the test below is not "the code still does what the code does". It is
"the path is still the path we looked at". When it fails it prints a
diff, and the only way past it is for a person to read that diff and say
whether the new path is better. `UPDATE_TRACE=1` rewrites the file, and
committing that rewrite is the act of agreeing.

Everything else here supports that one test. Faithful, because a trace
that rendered a raise as a return would be a trace of a different system.
Cheap, because a trace nobody can afford to leave on gets left off.
Repeatable, because a path that fingerprints differently in two processes
was never a path anyone could sign off — and because a fingerprint that
moves with `PYTHONHASHSEED` means a set's iteration order reached the
behaviour, which is what `sorted(t.resumes)` in the codec exists to stop.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import queue_mem
import store_mem
import tracing
from engine import Engine
from kernel import KernelCfg, PromiseGet
from runtime import Clock, Runtime, Worker
from spec import queue as queue_spec
from spec import store as store_spec
from test_e2e import AGENT, CALLS, EXPECTED, ORIGIN, QUESTION, SEARCH, agent, research, search

#: Where the code is, and where the tests are: a subprocess needs both
#: on its path to re-run one of these from outside pytest.
ROOT = Path(__file__).parent.parent
HERE = Path(__file__).parent

#: The path, as reviewed. Rewrite them with UPDATE_TRACE=1, and commit the
#: rewrite only once you have read the diff.
GOLDEN = HERE / "research.trace"

#: The same path as a picture. `SEQUENCE.md` was drawn by hand from what
#: the code was believed to do; this one is drawn from what it did, which
#: is a different kind of claim and worth keeping beside the other.
DIAGRAM = HERE / "research.mmd"

#: Every call is 208 arrows and nobody reads that. One level down from the
#: shell is the story: workers, transitions, and the ports the engine
#: reaches directly.
DIAGRAM_DEPTH = 1
CFG = KernelCfg(retry_timeout=30_000)


def world():
    """A lease shorter than the retry timeout, so the deadline actually
    moves during the run and the arm/disarm path is in the trace. With
    them equal — the old fixture — every deadline coincides and the timer
    is armed once for the whole run."""
    CALLS.clear()
    clock = Clock()
    store = tracing.watch(store_mem.Store(), store_spec.StoreP, "store")
    queue = tracing.watch(queue_mem.Queue(), queue_spec.QueueP, "queue")
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(AGENT, Worker(engine, clock, "agent-1", ttl=20_000), research, agent)
    rt.serve(SEARCH, Worker(engine, clock, "search-1", ttl=20_000), search)
    return rt, clock


def run() -> tracing.Trace:
    rt, clock = world()
    with tracing.recording() as t:
        rt.start(ORIGIN, research, QUESTION)
        for _ in range(12):
            rt.drain()
            clock.advance(40_000)
        rt.drain()
    return t


# --- cheap -----------------------------------------------------------------


def test_nothing_is_recorded_unless_someone_is_recording():
    rt, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    with tracing.recording() as t:
        pass
    assert t.calls == [], "a recording that began after the run saw the run"


def test_a_decorated_function_is_itself_when_nobody_is_watching():
    @tracing.trace
    def add(a, b=1):
        return a + b

    assert add(2) == 3 and add(2, b=5) == 7


# --- faithful --------------------------------------------------------------


def test_a_raise_is_recorded_as_a_raise():
    """The thing `sys.setprofile` cannot do: it reports a raise and a
    `return None` identically, and in this system a refusal is an
    outcome."""
    @tracing.trace
    def refuse():
        raise ValueError("no")

    @tracing.trace
    def returns_none():
        return None

    with tracing.recording() as t:
        returns_none()
        with pytest.raises(ValueError):
            refuse()
    assert [c.result for c in t.calls] == ["None", "!ValueError"]


def test_the_log_is_in_call_order_not_return_order():
    """Recording on return puts every child before its parent, which is
    the order a stack unwinds and the opposite of what happened."""
    t = run()
    first = t.calls[0]
    assert first.depth == 0 and first.name == "Engine.process"
    assert t.calls[1].depth == 1, "the parent's first call comes after the parent"


def test_the_run_is_what_the_trace_says_it_is():
    t = run()
    assert len(t.of("Engine.process")) == 22, "transitions"
    assert len(t.of("store.put")) == 18, "one conditional write per transition that changed something"
    assert len(t.of("store.get")) > len(t.of("store.put")), "reads are free"
    outer = t.of("Worker.execute_until_blocked_outer")
    assert [c.result for c in outer].count("'suspended'") >= 1
    inner = t.of("Worker.execute_until_blocked_inner")
    assert len(inner) >= len(outer) > 0, "every claim runs the function at least once"
    assert any(c.result == "!Blocked" for c in inner), \
        "the fan-out unwound through Blocked, out of the inner half"


def test_the_deadline_is_armed_and_disarmed_as_it_moves():
    """With a lease shorter than the retry timeout the timer transition
    happens for real, rather than every deadline landing on one instant."""
    t = run()
    arms = [c for c in t.of("queue.create") if "sweep/" in c.args]
    assert len(arms) > 1, "the deadline never moved; the fixture is too kind"
    assert t.of("queue.delete"), "armed and never disarmed"


def test_nothing_with_a_heap_address_reaches_a_trace():
    """The rule the fingerprint rests on, and the first thing to break it
    was a `Durable` passed to the inner half: `<sdk.Durable object at
    0x7ff99d975cd0>` is different in every process."""
    class Anonymous:
        pass

    with tracing.recording() as t:
        @tracing.trace
        def takes(thing):
            return thing
        takes(Anonymous())
    assert "0x" not in t.tree(), t.tree()
    assert "<Anonymous>" in t.tree()


def test_a_durable_function_says_which_one_it_is():
    t = run()
    args = t.of("Worker.execute_until_blocked_inner")[0].args
    assert "fn=@resonate research" in args, args


def test_a_document_body_is_in_the_trace_by_its_identity_not_its_bulk():
    t = run()
    put = t.of("store.put")[0]
    assert "body=<" in put.args and "b>" in put.args
    assert len(put.args) < 120, put.args


def test_every_call_knows_the_request_that_caused_it():
    with tracing.recording() as t:
        with tracing.because("POST /execute"):
            @tracing.trace
            def inner():
                return 1
            inner()
    assert [c.where for c in t.calls] == ["POST /execute"]


def test_the_service_labels_a_trace_with_the_route(monkeypatch):
    import app

    monkeypatch.setenv("SIMULATED", "1")
    import local
    local.reset()
    service = app.from_environment()
    with tracing.recording() as t:
        service.handle("POST", "/", {"kind": "promise.get", "data": {"id": "nothing"}})
    assert t.calls and all(c.where == "POST /" for c in t.calls)
    assert t.of("Engine.process")[0].result.startswith("Reply(status=404")


# --- the path, as reviewed -------------------------------------------------


def test_the_path_through_the_system_is_the_one_we_reviewed():
    t = run()
    got = t.tree()
    if os.environ.get("UPDATE_TRACE"):  # pragma: no cover - a person, deliberately
        GOLDEN.write_text(got + "\n")
        DIAGRAM.write_text(t.sequence(depth=DIAGRAM_DEPTH) + "\n")
        pytest.skip(f"rewrote {GOLDEN.name} and {DIAGRAM.name}; "
                    "read the diff before committing it")
    want = GOLDEN.read_text().rstrip("\n")
    if got == want:
        return
    diff = "\n".join(difflib.unified_diff(
        want.split("\n"), got.split("\n"), "reviewed", "now", lineterm="", n=3))
    pytest.fail("the path through the system changed.\n"
                "Read this, decide whether the new path is right, and if it is, "
                "rerun with UPDATE_TRACE=1 and commit the rewrite.\n\n" + diff)


def test_the_reviewed_path_says_what_we_think_it_says():
    """A golden file nobody can read is a golden file nobody reviews, so
    these are the claims a reader should be able to see in it."""
    want = GOLDEN.read_text()
    assert want.count("\u2192 ") == want.count("\u2190 "), \
        "every call came back, or the trace is lying about something"
    assert want.count("queue.create(url='worker://search'") == 3, \
        "the fan-out dispatches three searches"
    assert want.count("= !Blocked") == 2, "and then blocks, out through both halves"
    assert want.count(", 'v15')") == 5 and want.count("if_match='v15'") == 1, \
        "the replay reads five promises back at one version and writes once"
    assert want.count("url='sweep/research.1'") == 10 and want.count("\u2192 queue.delete(") == 10, \
        "the deadline is re-armed and the old one collected, every time it moves"


def test_the_diagram_is_drawn_from_the_same_run():
    """Two renderings of one recording, so they cannot drift: if the path
    changes and only the text is regenerated, this says so."""
    assert DIAGRAM.read_text().rstrip("\n") == run().sequence(depth=DIAGRAM_DEPTH)


def test_the_diagram_is_a_diagram():
    mmd = DIAGRAM.read_text()
    assert mmd.startswith("sequenceDiagram\n")
    assert mmd.count("->>+") == mmd.count("-->>-"), \
        "an arrow out with no arrow back would draw a lifeline that never closes"


def test_participants_are_ordered_by_who_calls_whom():
    """Left to right, so every arrow points right and crosses nothing.
    First appearance would put the worker to the right of the ports it
    only ever reaches through the engine."""
    order = [line.split()[-1] for line in DIAGRAM.read_text().split("\n")
             if line.strip().startswith("participant ")]
    assert order == ["Runtime", "Worker", "Engine", "store", "queue"], order


def test_cutting_the_diagram_off_never_leaves_a_dangling_arrow():
    """Both events of a call carry the same depth, so a cut takes the
    arrow out and the arrow back together or neither."""
    t = run()
    for depth in (0, 1, 2, None):
        mmd = t.sequence(depth=depth)
        assert mmd.count("->>+") == mmd.count("-->>-"), depth


# --- repeatable ------------------------------------------------------------

STABILITY = """
import sys
sys.path.insert(0, %r)
sys.path.insert(0, %r)
import test_tracing
print(test_tracing.run().fingerprint())
""" % (str(HERE), str(ROOT))


@pytest.mark.parametrize("seed", ["0", "1", "2"])
def test_the_fingerprint_is_the_same_in_any_process(seed):
    """Across hash seeds, because a set of strings iterates differently in
    every process and any such order leaking into the trace would make a
    golden trace worthless — and would mean it had leaked into the bytes
    too."""
    done = subprocess.run([sys.executable, "-c", STABILITY], cwd=ROOT,
                          capture_output=True, text=True,
                          env={"PYTHONHASHSEED": seed, "PATH": ""})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == run().fingerprint(), done.stdout


def test_a_different_run_has_a_different_fingerprint():
    """A fingerprint that never moves is not a fingerprint."""
    one = run()
    rt, clock = world()
    with tracing.recording() as two:
        rt.engine.process(PromiseGet("nothing"), 0)
    assert one.fingerprint() != two.fingerprint()
