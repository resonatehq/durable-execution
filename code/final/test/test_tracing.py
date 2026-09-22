"""A trace is worth having only if it is faithful, cheap and repeatable.

Faithful: it records a raise as a raise, in the order things happened.
Cheap: it costs nothing when nobody is recording.
Repeatable: the same run fingerprints the same, in any process.

That last one is the property the whole idea rests on. If a fingerprint
moved between runs, a golden trace would be noise — and it is also the
check that would catch a set's iteration order leaking into behaviour,
which is the one thing `sorted(t.resumes)` in the codec is there to stop.
"""

from __future__ import annotations

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
