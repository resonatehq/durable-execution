"""The whole thing, from a decorated function to bytes in a bucket.

The program is the one from the repository's own README, character for
character: plan the searches, fan them out, synthesize the results. It is
ordinary async/await. Nothing in it mentions promises, tasks, leases,
retries or recovery, which is the claim the entire project is making.

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

from resonate import properties as P
from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg, PromiseRegisterListener, Send
from resonate.ports import Crash, Fault
from resonate.queue_mem import Queue
from resonate.spec.queue import SWEEP
from resonate.runtime import Clock, Runtime, Worker
from resonate.store_mem import Store
from resonate.wire import decode_message
from resonate.sdk import REGISTRY, Failed, gather, resonate

CFG = KernelCfg(retry_timeout=30_000)
AGENT, SEARCH = "worker://agent", "worker://search"
VALIDATOR = jsonschema.Draft202012Validator(
    json.loads((Path(__file__).parent.parent / "line.schema.json").read_text()))

#: What the model and the search index were actually asked to do. The point
#: of the whole exercise is that these do not grow on replay.
CALLS: Counter = Counter()


@resonate
async def agent(prompt: str):
    """A model call. Async, because that is what a model call is."""
    CALLS["agent"] += 1
    if prompt.startswith("Plan"):
        return ["durable execution", "workflow recovery", "sagas"]
    # The synthesized prompt, verbatim, so a test can see that every search
    # result actually reached the step that was supposed to cite it.
    return {"report": prompt}


@resonate
def search(query: str):
    """A leaf with nothing to await. It does not have to pretend."""
    CALLS["search:" + query] += 1
    return f"finding about {query}"


@resonate
async def research(question: str):
    # Plan the searches
    queries = await agent(f"Plan the searches for: {question}")

    # Fan out the searches
    results = await gather(search.rpc(q) for q in queries)

    # Synthesize the results
    return await agent(f"Write a cited report. {question}: {results}")


QUESTION = "What is durable execution?"
ORIGIN = "research.1"


def world(fault: Fault | None = None):
    """One store, one queue, one clock, two workers."""
    CALLS.clear()
    # `@resonate` writes to a single global registry at import, and
    # `main.py` -- the deployed example -- defines functions by these same
    # names. Whichever module imported last owns them, so this file's
    # versions, the ones that count their calls, are claimed here rather
    # than left to the order pytest happens to collect in.
    for fn in (research, agent, search):
        REGISTRY[fn.name] = fn
    store, queue, clock = Store(fault), Queue(fault=fault), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(AGENT, Worker(engine, clock, "agent-1"), research, agent)
    rt.serve(SEARCH, Worker(engine, clock, "search-1"), search)
    return rt, store, engine, clock


def dispatched(rt) -> list[Send]:
    """Every message the run put on the queue, decoded. A deadline is a
    task too, and it is not one of these: what tells them apart is where
    they are addressed."""
    return [Send(url, decode_message(body)) for url, body in rt.queue.created
            if not url.startswith(SWEEP)]


def document(store: Store, origin: str = ORIGIN):
    found = store.get(doc_key(origin))
    return decode(found[0].encode(), origin) if found else None


def check_bytes(store: Store) -> None:
    """Every line of every document in the bucket, against the schema."""
    for key, (body, _) in store.objects.items():
        for i, line in enumerate(body.split("\n")):
            errs = list(VALIDATOR.iter_errors(json.loads(line)))
            assert not errs, f"{key} line {i}: {[e.message for e in errs]}"


def answer(store: Store, origin: str = ORIGIN):
    doc = document(store, origin)
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
    rt, store, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    assert answer(store) == EXPECTED
    check_bytes(store)


def test_nothing_is_paid_for_twice():
    """Three searches, two model calls, each exactly once, across however
    many times the function was re-run from the top."""
    rt, store, _, _ = world()
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
    rt, store, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    doc = document(store)
    branches = [o for o in doc.objects if o.id.startswith(f"{ORIGIN}:") and o.task is not None]
    assert len(branches) == 3, [o.id for o in doc.objects]
    # The run's own task was claimed twice: once to plan and fan out, once to
    # synthesize after the wake. Not four times, which is what suspending on
    # each branch separately would have cost.
    assert doc.get(ORIGIN).task.version == 2


def test_a_listener_is_told_when_the_run_settles():
    rt, store, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    engine.process(PromiseRegisterListener(ORIGIN, "http://client"), clock())
    rt.drain()
    assert ORIGIN in rt.notified
    assert rt.notified[ORIGIN]["state"] == "resolved"


def test_the_bucket_holds_one_document_for_the_whole_run():
    """Every promise and task of this run is one object under one key, which
    is why one conditional write commits a whole transition."""
    rt, store, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    assert list(store.objects) == [doc_key(ORIGIN)]
    doc = document(store)
    assert {o.id for o in doc.objects} == {
        ORIGIN, f"{ORIGIN}:1", f"{ORIGIN}:2", f"{ORIGIN}:3", f"{ORIGIN}:4", f"{ORIGIN}:5"}


def test_every_state_the_run_passes_through_is_one_the_catalogue_admits():
    """The conformance catalogue, over a real program rather than a script
    somebody wrote to be graded."""
    rt, store, engine, clock = world()
    rt.start(ORIGIN, research, QUESTION)
    seen = P.State(document(store), retry_timeout=CFG.retry_timeout)
    steps = 0
    while rt.step():
        steps += 1
        seen = seen.after(document(store), dispatched(rt))
        assert P.state_failures(clock(), seen) == [], P.state_failures(clock(), seen)
    assert steps >= 3 and answer(store) == EXPECTED


# --- it survives ------------------------------------------------------------


def clean_run() -> tuple[object, Counter]:
    rt, store, _, _ = world()
    rt.start(ORIGIN, research, QUESTION)
    rt.drain()
    return answer(store), Counter(CALLS)


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
    rt, store, engine, clock = world(fault)
    rt.start(ORIGIN, research, QUESTION)
    fault.crash_after(k)
    try:
        rt.drain()
    except Crash:
        pass
    else:
        pytest.skip(f"the run performs fewer than {k + 1} writes")

    assert document(store) is None or P.state_failures(clock(), P.State(document(store))) == []
    fault.heal()
    for _ in range(6):  # each round: let a deadline come due, then work it
        clock.advance(40_000)
        rt.drain()
    assert answer(store) == want
    check_bytes(store)

    extra = sum((Counter(CALLS) - clean).values())
    assert extra <= 1, f"more than the in-flight call was repeated: {CALLS} vs {clean}"


# --- the rest of the programming model -------------------------------------

@resonate
def double(x: int):
    CALLS["double"] += 1
    return x * 2


@resonate
async def middle(x: int):
    return await double(x) + 1


@resonate
async def three_deep(x: int):
    return await middle(x) + await middle(x + 10)


@resonate
async def fan_out_locally(n: int):
    return await gather(double(i) for i in range(n))


@resonate
def on_fire(x: int):
    CALLS["on_fire"] += 1
    raise ValueError("the index is on fire")


@resonate
async def calls_something_broken(x: int):
    return await on_fire(x)


@resonate
async def survives_a_broken_call(x: int):
    try:
        return await on_fire(x)
    except Failed as e:
        return f"carried on after {e}"


def run(fn, id, *args, extra=()):
    rt, store, engine, clock = world()
    rt.serve(AGENT, Worker(engine, clock, "w"), fn, *extra)
    rt.start(id, fn, *args)
    rt.drain()
    return decode(store.get(doc_key(id))[0].encode(), id)


def test_a_durable_call_can_contain_one():
    """Three levels, and the ids say so: a call made from inside `:1` is
    `:1.1`, which sorts under it and nowhere else."""
    doc = run(three_deep, "nest.1", 5, extra=(middle, double))
    assert json.loads(doc.get("nest.1").promise.value.data) == 42
    assert [o.id for o in doc.objects] == [
        "nest.1", "nest.1:1", "nest.1:1.1", "nest.1:2", "nest.1:2.1"]


def test_gather_works_over_local_calls_too():
    doc = run(fan_out_locally, "local.1", 3, extra=(double,))
    assert json.loads(doc.get("local.1").promise.value.data) == [0, 2, 4]
    assert [o.id for o in doc.objects] == ["local.1", "local.1:1", "local.1:2", "local.1:3"]


def test_a_call_that_raises_is_a_rejection_not_a_crash():
    """Post 001: an exception settles the promise, it does not escape. The
    rejection carries what went wrong, and it propagates to the caller as
    the caller's own rejection."""
    doc = run(calls_something_broken, "boom.1", 1, extra=(on_fire,))
    leaf, root = doc.get("boom.1:1").promise, doc.get("boom.1").promise
    assert leaf.state == "rejected"
    assert json.loads(leaf.value.data) == {
        "type": "ValueError", "message": "the index is on fire"}
    assert root.state == "rejected"
    assert "the index is on fire" in json.loads(root.value.data)["message"]


def test_a_rejection_can_be_caught_and_carried_on_from():
    """Which is the point of recording it as a result rather than throwing
    it away: the caller decides what a failure means."""
    doc = run(survives_a_broken_call, "saga.1", 1, extra=(on_fire,))
    root = doc.get("saga.1").promise
    assert root.state == "resolved"
    assert "the index is on fire" in json.loads(root.value.data)


def test_a_rejection_is_read_back_rather_than_re_raised_by_running_again():
    """The expensive half of the claim. A call that failed is not called a
    second time to discover that it fails; the rejection is the result, and
    replay reads results."""
    fault = Fault()
    rt, store, engine, clock = world(fault)
    rt.serve(AGENT, Worker(engine, clock, "w"), calls_something_broken, on_fire)
    rt.start("boom.2", calls_something_broken, 1)
    # Stop after the rejection is committed but before the run settles.
    fault.crash_after(6)
    try:
        rt.drain()
    except Crash:
        pass
    called_once = CALLS["on_fire"]
    fault.heal()
    for _ in range(6):
        clock.advance(40_000)
        rt.drain()
    doc = decode(store.get(doc_key("boom.2"))[0].encode(), "boom.2")
    assert doc.get("boom.2").promise.state == "rejected"
    assert CALLS["on_fire"] == called_once, "the failing call was made again"


def test_another_worker_finishes_what_a_dead_one_started():
    """A run is not a process. The lease expires, the task is offered again,
    and whoever takes it runs the function from the top over the promises
    the first worker managed to settle."""
    want, _ = clean_run()
    fault = Fault()
    rt, store, engine, clock = world(fault)
    rt.start(ORIGIN, research, QUESTION)
    fault.crash_after(6)
    try:
        rt.drain()
    except Crash:
        pass
    first = rt.workers[AGENT]
    fault.heal()
    second = Worker(engine, clock, "agent-2")
    rt.serve(AGENT, second, research, agent)
    for _ in range(6):
        clock.advance(40_000)
        rt.drain()
    assert answer(store) == want
    assert second.ran, "the second worker never picked anything up"
    assert second is not first
