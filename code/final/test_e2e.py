"""The whole thing, from a decorated function to bytes in a bucket.

The program is the one from the repository's own README: plan the searches,
fan them out, synthesize the results. It is ordinary Python. Nothing in it
mentions promises, tasks, leases, retries or recovery, which is the claim
the entire project is making.

Everything under it is real: the kernel decides, the engine commits one
conditional write per transition, the document lands in a simulated bucket
as canonical lines, and a runtime carries the messages and fires the
deadlines. Only the bucket, the queue and the clock are in memory, and each
is behind the port its production version will implement.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

import properties as P
from blob import BlobStore, MemoryBlob
from codec import decode, doc_key
from engine import Engine
from kernel import KernelCfg, PromiseRegisterListener, Send
from ports import Crash, Fault, MemoryTimers, MemoryTransport
from runtime import Clock, Runtime, Worker
from sdk import REGISTRY, gather, resonate

CFG = KernelCfg(retry_timeout=30_000)
AGENT, SEARCH = "worker://agent", "worker://search"
VALIDATOR = jsonschema.Draft202012Validator(
    json.loads((Path(__file__).parent / "line.schema.json").read_text()))

#: What the model and the search index were actually asked to do. The point
#: of the whole exercise is that these do not grow on replay.
CALLS: Counter = Counter()


@resonate(target=AGENT)
def agent(prompt: str):
    CALLS["agent"] += 1
    if prompt.startswith("Plan"):
        return ["durable execution", "workflow recovery", "sagas"]
    # The synthesized prompt, verbatim, so a test can see that every search
    # result actually reached the step that was supposed to cite it.
    return {"report": prompt}


@resonate(target=SEARCH)
def search(query: str):
    CALLS["search:" + query] += 1
    return f"finding about {query}"


@resonate(target=AGENT)
def research(question: str):
    # Plan the searches
    queries = agent(f"Plan the searches for: {question}")
    # Fan out the searches
    results = gather(*[search.rpc(q) for q in queries])
    # Synthesize the results
    return agent(f"Write a cited report. {question}: {results}")


QUESTION = "What is durable execution?"
ORIGIN = "research.1"


def world(fault: Fault | None = None):
    """One bucket, one queue, one clock, two workers."""
    CALLS.clear()
    blob = MemoryBlob(fault)
    store, timers, transport = BlobStore(blob), MemoryTimers(fault), MemoryTransport(fault)
    clock = Clock()
    engine = Engine(store, timers, transport, CFG)
    rt = Runtime(engine, timers, transport, clock)
    rt.serve(AGENT, Worker(engine, clock, "agent-1"))
    rt.serve(SEARCH, Worker(engine, clock, "search-1"))
    return rt, blob, engine, clock


def document(blob: MemoryBlob, origin: str = ORIGIN):
    found = blob.get(doc_key(origin))
    return decode(found[0].encode(), origin) if found else None


def check_bytes(blob: MemoryBlob) -> None:
    """Every line of every document in the bucket, against the schema."""
    for key, (body, _) in blob._objects.items():
        for i, line in enumerate(body.split("\n")):
            errs = list(VALIDATOR.iter_errors(json.loads(line)))
            assert not errs, f"{key} line {i}: {[e.message for e in errs]}"


def answer(blob: MemoryBlob, origin: str = ORIGIN):
    doc = document(blob, origin)
    root = doc.get(origin)
    assert root.promise.state == "resolved", f"the run did not finish: {root.promise.state}"
    return json.loads(root.promise.value.data)


# --- it runs ---------------------------------------------------------------


#: What the program returns when nothing goes wrong. Every result of the
#: fan-out is in it, which is what says the branches reached the step that
#: cites them.
EXPECTED = {"report": (
    "Write a cited report. What is durable execution?: "
    "['finding about durable execution', 'finding about workflow recovery', "
    "'finding about sagas']")}


def test_the_research_agent_runs_to_completion():
    rt, blob, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    assert answer(blob) == EXPECTED
    check_bytes(blob)


def test_nothing_is_paid_for_twice():
    """Three searches, two model calls, each exactly once, across however
    many times the function was re-run from the top."""
    rt, blob, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    assert dict(CALLS) == {
        "agent": 2,
        "search:durable execution": 1,
        "search:workflow recovery": 1,
        "search:sagas": 1,
    }


def test_the_fan_out_is_a_fan_out():
    """All three searches are dispatched before anything blocks, and the
    caller suspends once rather than once per branch. Dispatching and
    reading are separable for exactly this reason."""
    rt, blob, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    doc = document(blob)
    branches = [o for o in doc.objects if o.id.startswith(f"{ORIGIN}:") and o.task is not None]
    assert len(branches) == 3, [o.id for o in doc.objects]
    # The run's own task was claimed twice: once to plan and fan out, once to
    # synthesize after the wake. Not four times, which is what suspending on
    # each branch separately would have cost.
    assert doc.get(ORIGIN).task.version == 2


def test_a_listener_is_told_when_the_run_settles():
    rt, blob, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    engine.process(PromiseRegisterListener(ORIGIN, "http://client"), clock())
    rt.drain()
    assert ORIGIN in rt.notified
    assert rt.notified[ORIGIN]["state"] == "resolved"


def test_the_bucket_holds_one_document_for_the_whole_run():
    """Every promise and task of this run is one object under one key, which
    is why one conditional write commits a whole transition."""
    rt, blob, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    assert list(blob._objects) == [doc_key(ORIGIN)]
    doc = document(blob)
    assert {o.id for o in doc.objects} == {
        ORIGIN, f"{ORIGIN}:1", f"{ORIGIN}:2", f"{ORIGIN}:3", f"{ORIGIN}:4", f"{ORIGIN}:5"}


def test_every_state_the_run_passes_through_is_one_the_catalogue_admits():
    """The conformance catalogue, over a real program rather than a script
    somebody wrote to be graded."""
    rt, blob, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    seen = P.State(document(blob), retry_timeout=CFG.retry_timeout)
    steps = 0
    while rt.step():
        steps += 1
        seen = seen.after(document(blob), [Send(a, m) for a, m in rt.transport.sent])
        assert P.state_failures(clock(), seen) == [], P.state_failures(clock(), seen)
    assert steps >= 3 and answer(blob) == EXPECTED


# --- it survives ------------------------------------------------------------


def clean_run() -> tuple[object, Counter]:
    rt, blob, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    return answer(blob), Counter(CALLS)


@pytest.mark.parametrize("k", range(28))
def test_killing_the_worker_at_any_write_still_finishes_the_run(k):
    """Cut the power at the k-th write anywhere in the system, then let the
    world do what it does: the lease expires, the task is offered again, and
    the function runs from the top. Every step that committed is read back
    rather than redone.

    The one thing that is *not* claimed is exactly-once side effects. A
    process can die between a call finishing and its settle committing, and
    nothing can close that window over a network. What is claimed is that at
    most the in-flight call is repeated, and that the answer is the same.
    """
    want, clean = clean_run()

    fault = Fault()
    rt, blob, engine, clock = world(fault)
    rt.start(ORIGIN, research, QUESTION)
    fault.crash_after(k)
    try:
        rt.drain()
    except Crash:
        pass
    else:
        pytest.skip(f"the run performs fewer than {k + 1} writes")

    assert document(blob) is None or P.state_failures(clock(), P.State(document(blob))) == []
    fault.heal()
    for _ in range(6):  # each round: let a deadline come due, then work it
        clock.advance(40_000)
        rt.drain()
    assert answer(blob) == want
    check_bytes(blob)

    extra = sum((Counter(CALLS) - clean).values())
    assert extra <= 1, f"more than the in-flight call was repeated: {CALLS} vs {clean}"
