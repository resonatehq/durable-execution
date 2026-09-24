"""The translated agent, driven through a whole booking.

`examples/travel-agent/` is a translation of Temporal's durable-AI-agent
tutorial. An example nobody runs rots, and this one makes claims that are
easy to get subtly wrong -- that a person really does gate the step that
changes something, that declining really does stop it, that a model naming
a tool this agent does not have is refused rather than crashed on. So the
whole conversation runs here, end to end, over the simulated ports.

It runs with no Anthropic credentials: `planner.py` falls back to a
scripted planner, which is the only way a test of *the agent* is a test of
the agent and not of the weather inside a model.
"""

from __future__ import annotations

import json
import sys
from importlib import util
from pathlib import Path

import pytest

from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg, TAG_EXTERNAL
from resonate.types import PromiseSettle
from resonate.testing.queue_mem import Queue
from resonate.testing.sim import Clock, Runtime
from resonate.worker import Worker
from resonate.sdk import dumps
from resonate.testing.store_mem import Store

EXAMPLE = Path(__file__).parent.parent / "examples" / "travel-agent"
CFG = KernelCfg(retry_timeout=30_000)
WORKER = "worker://agent"
ORIGIN = "trip.1"


@pytest.fixture(scope="module")
def agent():
    """The example, loaded the way the platform loads it.

    Under a name of its own: this repository's own `main.py` is already
    imported as `main` by other tests, and two modules cannot both be it.
    """
    sys.path.insert(0, str(EXAMPLE))
    try:
        spec = util.spec_from_file_location("travel_agent", EXAMPLE / "main.py")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(EXAMPLE))


@pytest.fixture
def world(agent):
    store, queue, clock = Store(), Queue(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(WORKER, Worker(engine, clock, "agent-1"), agent.chat)
    return rt, store, engine, clock


def document(store, origin=ORIGIN):
    found = store.get(doc_key(origin))
    return decode(found[0].encode()) if found else None


def pending_question(store):
    """What the agent is waiting for, found by reading the document -- which
    is what a UI would do, and what Temporal needs a query handler for."""
    doc = document(store)
    asking = [o for o in doc.objects
              if o.promise.tags.get(TAG_EXTERNAL) == "true"
              and o.promise.state == "pending"]
    if not asking:
        return None, None
    o = asking[0]
    return o.id, json.loads(o.promise.param.data)["ask"]


def answer(world, replies, limit=40):
    """Play the person. Returns the transcript of questions asked."""
    rt, store, engine, clock = world
    asked = []
    for _ in range(limit):
        rt.drain()
        id, question = pending_question(store)
        if id is None:
            return asked
        asked.append(question)
        if not replies:
            return asked
        engine.process(PromiseSettle(id, "resolved", dumps(replies.pop(0))), clock())
    raise AssertionError("the conversation never settled")


def finished(store):
    root = document(store).get(ORIGIN).promise
    return json.loads(root.value.data) if root.state == "resolved" else None


def tools_run(store):
    """Which tools actually ran, by reading what the run recorded."""
    doc = document(store)
    ran = []
    for o in doc.objects:
        param = json.loads(o.promise.param.data or "{}")
        if param.get("f") in ("find_events", "search_flights", "create_invoice"):
            ran.append(param["f"])
    return ran


BOOKING = ["an event in new york city in may",
           "san francisco",
           "me@example.com",
           "yes"]


def test_the_whole_booking(world, agent):
    """Four answers from a person, and a trip is booked."""
    rt, store, _, _ = world
    rt.start(ORIGIN, agent.chat)
    asked = answer(world, list(BOOKING))

    done = finished(store)
    assert done is not None, f"never finished; last asked: {asked[-1:]}"
    assert done["ended"] == "the goal is met", done["ended"]
    assert "invoices.example.com" in done["said"], done["said"]
    assert tools_run(store) == ["find_events", "search_flights", "create_invoice"]


def test_the_person_is_asked_before_anything_changes(world, agent):
    """The claim the whole confirmation step exists for. The two tools that
    only read are never put to the user; the one that invoices somebody
    always is."""
    rt, store, _, _ = world
    rt.start(ORIGIN, agent.chat)
    asked = answer(world, list(BOOKING))

    confirmations = [q for q in asked if isinstance(q, dict) and "confirm" in q]
    assert len(confirmations) == 1, confirmations
    assert confirmations[0]["confirm"] == "create_invoice"
    assert confirmations[0]["args"]["email"] == "me@example.com"
    assert confirmations[0]["args"]["flight"] == "AA101", "it did not price them"


def test_declining_actually_stops_it(world, agent):
    """A confirmation that can be declined and still runs the tool is not a
    confirmation. Nothing is invoiced, and the run ends without one."""
    rt, store, _, _ = world
    rt.start(ORIGIN, agent.chat)
    answer(world, ["an event in new york city in may", "san francisco",
                   "me@example.com", "no", "end"])

    assert "create_invoice" not in tools_run(store), "it invoiced them anyway"
    done = finished(store)
    assert done is not None and done["ended"] == "the user said so"


def test_a_question_is_a_promise_and_nothing_else_is_running(world, agent):
    """Between two messages there is a document in a bucket with one pending
    promise in it. No container, no coroutine, no held task -- draining
    again at the same instant does nothing at all."""
    rt, store, _, _ = world
    rt.start(ORIGIN, agent.chat)
    rt.drain()

    id, question = pending_question(store)
    assert question == {"for": "message"}, question
    assert id == f"{ORIGIN}:1", "the question's id is a position, like every call"
    assert rt.drain() == 0, "something was still running while it waited"


def test_the_history_is_rebuilt_not_stored(world, agent):
    """The part that looks like a bug.

    `chat` keeps its conversation in a local list that is thrown away at
    every suspension, and nothing ever writes it anywhere. It survives
    because every value in it came from a durable call: mid-conversation,
    the document holds the promises and nothing else, and the user's own
    words can be read straight back out of the ones they answered. The
    local list is a *view* of that, reconstructed on each replay.

    Asserted mid-conversation on purpose. The finished run returns the
    transcript as its result, so a completed document contains it for a
    reason that has nothing to do with how it was kept.
    """
    rt, store, _, _ = world
    rt.start(ORIGIN, agent.chat)
    answer(world, ["an event in new york city in may", "san francisco"])

    assert finished(store) is None, "it finished; nothing was mid-conversation"
    doc = document(store)
    assert "conversation" not in store.get(doc_key(ORIGIN))[0], \
        "the history is being stored as well as derived"

    # The user's words, read out of the promises they settled rather than
    # out of anything the run wrote down.
    from_promises = [
        json.loads(o.promise.value.data) for o in doc.objects
        if o.promise.tags.get(TAG_EXTERNAL) == "true"
        and o.promise.state == "resolved"]
    assert from_promises == ["an event in new york city in may", "san francisco"]


def test_a_tool_the_agent_does_not_have_is_refused(agent):
    """A plan arrives as data, over the same wire as everything else. It is
    checked against what these functions actually are before anything is
    called."""
    assert agent.unacceptable("rm_rf", {}) is not None
    assert "no such tool" in agent.unacceptable("rm_rf", {})
    assert agent.unacceptable("find_events", {"city": "NYC"}) == "missing month"
    assert "does not take" in agent.unacceptable(
        "find_events", {"city": "NYC", "month": "May", "and_also": "rm -rf /"})
    assert agent.unacceptable("find_events", {"city": "NYC", "month": "May"}) is None


def test_the_example_only_uses_the_published_surface(agent):
    """It is a user application by the same rules as any other: if it needed
    something a user could not import, the translation would be cheating."""
    src = (EXAMPLE / "main.py").read_text()
    assert "from resonate import" in src
    for private in ("resonate.sdk", "resonate.server", "resonate.engine",
                    "resonate.kernel", "resonate.codec"):
        assert private not in src, f"the example reaches into {private}"
