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
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from resonate import queue_mem
from resonate import store_mem
from resonate import tracing
from resonate.engine import Engine
from resonate.worker import Worker
from resonate.server import Server
from resonate.kernel import KernelCfg, TAG_TARGET
from resonate.testing.sim import Clock
from resonate.sdk import dumps, route
from resonate.spec import queue as queue_spec
from resonate.spec import store as store_spec
from test_e2e import (
    CALLS, ORIGIN, QUESTION, counted_agent, counted_research, counted_search,
)

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

#: What comes out of the picture, and why it is the ports rather than a
#: depth. The engine reaches them from depth 2, 3 and 5 in one run, so any
#: depth cut shows some port calls and hides others — and a lifeline
#: standing idle through five deliveries reads as "nothing happened here",
#: which is the one thing that is not true. They are leaves, so dropping
#: them orphans nothing, and `research.trace` has every one.
DIAGRAM_OMIT = ("store", "queue")

#: What calls `Server.handle` in production, whoever sent the request. The
#: note above each arrow says which route it was.
CALLER = "CloudRun"

#: Where this deployment answers. One service runs every function, which is
#: the smallest shape that is still the real one.
WORKER = "https://svc-abc.a.run.app/"

#: The hop the picture cannot show. A task created on the queue arrives
#: later as a new request, and Cloud Tasks makes that request from another
#: process — so between a `queue.create` and the arrival it caused there is
#: a gap with nothing in it. Better to name the gap than to draw an arrow
#: nobody recorded, or to leave a reader joining two halves by eye.
PREAMBLE = """the store and the queue are left out; research.trace has all 65 calls to them
a task created on the queue arrives later as a new request, delivered by
Cloud Tasks from another process — that hop is in neither"""
CFG = KernelCfg(retry_timeout=30_000)


def world():
    """One container, as it is deployed: `Server` over the two simulated
    ports, reached the way production reaches it.

    Driving `Runtime` instead would be shorter and would record a path
    that does not exist — it calls the worker directly, so `Server` never
    appears and the reviewed trace would be of the test harness rather
    than of the service.

    The lease is shorter than the retry timeout on purpose. With them
    equal every deadline in the run coincides, `old != new` is never true,
    and the arm/disarm path never happens at all.
    """
    CALLS.clear()
    clock = Clock()
    store = tracing.watch(store_mem.Store(), store_spec.StoreP, "store")
    queue = tracing.watch(queue_mem.Queue(), queue_spec.QueueP, "queue")
    for fn in (counted_research, counted_agent, counted_search):
        route(fn, WORKER)
    engine = Engine(store, queue, CFG)
    routes = Server(engine, Worker(engine, clock, pid="rev-1", ttl=20_000), clock)
    return routes, queue, clock


def deliver(routes, queue, clock, budget: int = 2_000) -> int:
    """Cloud Tasks, as the only thing it is: a POST to a URL."""
    for did in range(budget):
        d = queue.take(clock())
        if d is None:
            return did
        body, status = routes.dispatch("POST", "/", d.body, "")
        assert status == 200, (d.url, status, body)
        queue.ack(d, clock())
    raise AssertionError("the queue never ran out of eligible work")


def run() -> tracing.Trace:
    routes, queue, clock = world()
    with tracing.recording() as t:
        routes.protocol({
            "kind": "promise.create",
            "data": {"id": ORIGIN, "timeoutAt": 10 ** 12,
                     "param": {"data": dumps({"f": "counted_research", "a": [QUESTION]}).data},
                     "tags": {TAG_TARGET: WORKER}}})
        for _ in range(12):
            deliver(routes, queue, clock)
            clock.advance(40_000)
        deliver(routes, queue, clock)
    return t


# --- cheap -----------------------------------------------------------------


def test_nothing_is_recorded_unless_someone_is_recording():
    routes, queue, clock = world()
    routes.protocol({"kind": "promise.get", "data": {"id": "nothing"}})
    deliver(routes, queue, clock)
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
    assert first.depth == 0 and first.name == "Server.protocol"
    assert t.calls[1].depth == 1, "the parent's first call comes after the parent"


def test_the_run_is_what_the_trace_says_it_is():
    t = run()
    assert len(t.of("Engine.process")) == 22, "transitions"
    assert len(t.of("store.put")) == 18, "one conditional write per transition that changed something"
    assert len(t.of("store.get")) > len(t.of("store.put")), "reads are free"
    outer = t.of("Worker.run")
    assert [c.result for c in outer].count("'suspended'") >= 1
    inner = t.of("Worker._attempt")
    assert len(inner) >= len(outer) > 0, "every claim runs the function at least once"
    assert any(c.result == "!Blocked" for c in inner), \
        "the fan-out unwound through Blocked, out of the inner half"


def test_the_deadline_is_armed_and_disarmed_as_it_moves():
    """With a lease shorter than the retry timeout the timer transition
    happens for real, rather than every deadline landing on one instant."""
    t = run()
    arms = [c for c in t.of("queue.create") if "'kind': 'timeout'" in c.args]
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
    args = t.of("Worker._attempt")[0].args
    assert "fn=@resonate counted_research" in args, args
    # No heap address, and no version either: an unversioned function reads
    # as its bare name, so a project that never versions anything never
    # finds a version in its own trace. `@resonate(version=1)` would read
    # `@resonate counted_research@1`.
    assert "0x" not in args, args


def test_a_document_body_is_in_the_trace_by_its_identity_not_its_bulk():
    t = run()
    put = t.of("store.put")[0]
    assert "body=<" in put.args and "b>" in put.args
    assert len(put.args) < 120, put.args


def test_every_call_knows_the_request_that_caused_it():
    with tracing.recording() as t:
        with tracing.because("POST / execute"):
            @tracing.trace
            def inner():
                return 1
            inner()
    assert [c.where for c in t.calls] == ["POST / execute"]


def test_the_entry_point_is_where_a_trace_starts():
    """A route is the outermost frame — and the only one with no cause of
    its own, because it is the cause. Everything under it carries the route.

    The route rather than a router: `handler` picks which of the four this
    is, from a Flask request whose repr carries a heap address, and nothing
    with an address in it is recordable. So the trace starts one call in,
    at the thing that was actually asked for, which is the more useful
    name anyway."""
    from resonate import config

    routes = config.build({"SIMULATED": "1"})
    with tracing.recording() as t:
        routes.protocol({"kind": "promise.get", "data": {"id": "nothing"}})

    first, rest = t.calls[0], t.calls[1:]
    assert first.name == "Server.protocol" and first.depth == 0
    assert first.where == "", "the entry point is caused by nothing inside this system"
    assert rest and all(c.where == "POST /" for c in rest)
    assert t.of("Engine.process")[0].result.startswith("Reply(status=404")
    assert "[POST /]" in t.tree(), "and the trace says so out loud"


# --- the path, as reviewed -------------------------------------------------


def test_the_path_through_the_system_is_the_one_we_reviewed():
    t = run()
    got = t.tree()
    if os.environ.get("UPDATE_TRACE"):  # pragma: no cover - a person, deliberately
        GOLDEN.write_text(got + "\n")
        DIAGRAM.write_text(t.sequence(omit=DIAGRAM_OMIT, caller=CALLER, preamble=PREAMBLE) + "\n")
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
    assert want.count("url='https://svc-abc") == 5, \
        "five dispatches: the run, three searches, and the run again"
    assert want.count("= !Blocked") == 2, "and then blocks, out through both halves"
    assert want.count(", 'v15')") == 5 and want.count("if_match='v15'") == 1, \
        "the replay reads five promises back at one version and writes once"
    assert want.count("'kind': 'timeout', 'origin': 'research.1'") == 10 and want.count("\u2192 queue.delete(") == 10, \
        "the deadline is re-armed and the old one collected, every time it moves"


def test_the_diagram_is_drawn_from_the_same_run():
    """Two renderings of one recording, so they cannot drift: if the path
    changes and only the text is regenerated, this says so."""
    assert DIAGRAM.read_text().rstrip("\n") == run().sequence(
        omit=DIAGRAM_OMIT, caller=CALLER, preamble=PREAMBLE)


def test_every_delivery_says_what_caused_it():
    """Six arrivals: one client request and five queue deliveries. They
    are separate arrivals even though five share a kind, so the heading
    prints per arrival rather than per distinct kind."""
    trace = GOLDEN.read_text()
    assert trace.count("[POST /]") == 1, "the client starting the run"
    assert trace.count("[POST / execute]") == 5, "and five deliveries from the queue"
    assert "[POST / timeout" not in trace, "no deadline fired; nothing ran late"
    mmd = DIAGRAM.read_text()
    # One protocol request and five dispatches, each named by the method
    # `dispatch` chose: the trace starts at what was chosen.
    assert mmd.count("CloudRun->>+Server: protocol(") == 1
    assert mmd.count("CloudRun->>+Server: execute(") == 5


def test_the_ports_are_left_out_rather_than_left_idle():
    """They are reached from three different depths, so any depth cut
    would show some and hide others — and a lifeline that sits idle
    through five deliveries says nothing happened on it."""
    mmd = DIAGRAM.read_text()
    assert "participant store" not in mmd and "participant queue" not in mmd
    assert "store" not in mmd.split("Note over")[-1].split("\n", 1)[1], \
        "not a single arrow to a port"
    # Dropping a leaf cannot orphan anything.
    assert mmd.count("->>+") == mmd.count("-->>-")
    assert "research.trace has all 65" in mmd, "and the banner says where they are"


def test_the_diagram_names_the_hop_it_cannot_show():
    """Between `queue.create` and the arrival that task caused there is
    nothing, because Cloud Tasks delivers it from another process. A
    reader who joins those two halves by eye is guessing; the banner says
    what the gap is."""
    mmd = DIAGRAM.read_text()
    assert "Note over CloudRun,Engine:" in mmd
    assert "another process" in mmd
    assert "CloudRun->>+Server: execute(" in mmd


def test_mermaid_can_actually_read_the_diagram():
    """The one check that is not me marking my own homework.

    Everything else here asks whether the file obeys rules this project
    invented — arrows balance, participants ordered, ports absent. None of
    them notice a file Mermaid refuses. A banner containing a semicolon
    once shipped broken because I had been rendering by hand after every
    change and, the one time I did not, nothing failed.

    Skipped where `npx` is unavailable, so it costs an offline machine
    nothing and a machine with a network the truth.
    """
    if shutil.which("npx") is None:  # pragma: no cover - no node here
        pytest.skip("needs npx to run mermaid")
    with tempfile.TemporaryDirectory() as tmp:
        # mermaid-cli drives a headless browser, which refuses to start as
        # root without this. Not a security decision: it renders one local
        # file we just wrote.
        config = Path(tmp) / "puppeteer.json"
        config.write_text(json.dumps({"args": ["--no-sandbox"]}))
        done = subprocess.run(
            ["npx", "-y", "@mermaid-js/mermaid-cli@11", "-p", str(config),
             "-i", str(DIAGRAM), "-o", str(Path(tmp) / "check.svg")],
            capture_output=True, text=True, timeout=600)
    if "Parse error" in done.stdout + done.stderr:
        pytest.fail("mermaid cannot read the generated diagram:\n"
                    + (done.stdout + done.stderr).split("Expecting")[0])
    if done.returncode != 0 and "Failed to launch" in done.stdout + done.stderr:
        pytest.skip("mermaid is installed but cannot start a browser here")
    assert done.returncode == 0, done.stdout + done.stderr


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
    assert order == ["CloudRun", "Server", "Worker", "Durable", "Engine"], order


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
    routes, _, _ = world()
    with tracing.recording() as two:
        routes.protocol({"kind": "promise.get", "data": {"id": "nothing"}})
    assert one.fingerprint() != two.fingerprint()
