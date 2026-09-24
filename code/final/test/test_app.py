"""The service, driven the way Cloud Run drives it.

Nothing here calls the engine. Every test goes in through a method, a path
and a JSON body, and comes back a status code, because that is the only
surface production has. What the earlier suites prove about the kernel and
the engine is not re-proved; what is proved here is the thin, dull layer
that is nevertheless the one a deployment gets wrong: which route, which
code, and who is allowed to knock.

The one thing this file cannot reach is the GCP half of `config.build` and
`Server.authorized`, which need credentials. They are wiring, and they are
marked as such.
"""

from __future__ import annotations

import json

import pytest

from resonate.engine import Engine
from resonate.worker import Worker
from resonate.server import Server
from resonate.codec import doc_key
from resonate.kernel import KernelCfg, TAG_TARGET
from resonate.testing.sim import Clock
from resonate.sdk import dumps, route
from resonate.testing.store_mem import Store
from test_e2e import (
    CALLS, EXPECTED, ORIGIN, QUESTION, counted_agent, counted_research,
    counted_search,
)
from resonate.testing.queue_mem import Queue
from resonate.types import Timeout

CFG = KernelCfg(retry_timeout=30_000)

#: Where this service answers. One service runs every function here, which
#: is the smallest deployment that is still the real shape: the queue calls
#: back in over HTTP rather than handing anything to a loop.
WORKER = "https://svc-abc.a.run.app/"


def service(**knobs):
    """One container instance, one store, one queue."""
    CALLS.clear()
    store, queue, clock = Store(), Queue(**knobs), Clock()
    engine = Engine(store, queue, CFG)
    svc = Server(engine, Worker(engine, clock, pid="rev-1", ttl=60_000), clock)
    for fn in (counted_research, counted_agent, counted_search):
        route(fn, WORKER)
    return svc, store, queue, clock


def deliver(svc: Server, queue: Queue, clock: Clock, budget: int = 2_000) -> int:
    """Cloud Tasks, as the only thing it is: a POST of the task's body."""
    for did in range(budget):
        d = queue.take(clock())
        if d is None:
            return did
        body, status = svc.dispatch("POST", "/", d.body, "")
        assert status == 200, (d.url, status, body)
        queue.ack(d, clock())
    raise AssertionError("the queue never ran out of eligible work")


def settle(svc, queue, clock, rounds: int = 12) -> None:
    for _ in range(rounds):
        deliver(svc, queue, clock)
        clock.advance(40_000)
    deliver(svc, queue, clock)


def post(svc: Server, kind: str, **data):
    return svc.protocol({"kind": kind, "data": data})


# --- the protocol endpoint -------------------------------------------------


def test_a_client_creates_and_reads_a_promise_over_the_wire():
    svc, _, _, clock = service()
    body, status = post(svc, "promise.create", id="p.1", timeoutAt=clock() + 1_000)
    assert status == 200 and body["data"]["promise"]["state"] == "pending"

    again, status = post(svc, "promise.get", id="p.1")
    assert status == 200 and again["data"]["promise"] == body["data"]["promise"]


def test_the_status_the_kernel_chose_is_the_status_the_client_sees():
    svc, _, _, _ = service()
    body, status = post(svc, "promise.get", id="nothing")
    assert status == 404 and body["head"]["status"] == 404


@pytest.mark.parametrize("envelope", [
    {},
    {"kind": "promise.get"},
    {"kind": "promise.get", "data": []},
    {"kind": "promise.get", "data": {}},
    {"kind": "promise.create", "data": {"id": "p.1", "timeoutAt": "soon"}},
    {"kind": "nonsense", "data": {}},
])
def test_a_malformed_request_is_a_400_and_never_reaches_the_bucket(envelope):
    svc, store, _, _ = service()
    body, status = svc.protocol(envelope)
    assert status == 400, body
    assert store.objects == {}, "a request that was never understood wrote something"


# --- the queue's routes ----------------------------------------------------


def test_a_duplicate_dispatch_is_answered_rather_than_retried():
    """At-least-once means the same `execute` arrives twice. The second
    finds the task claimed at a version it does not hold, and that refusal
    is a 200: delivering it a third time would not change anything."""
    svc, _, queue, clock = service()
    post(svc, "promise.create", id="w.1", timeoutAt=clock() + 1_000_000,
         param={"data": json.dumps({"f": "counted_search", "a": ["sagas"]})},
         tags={TAG_TARGET: WORKER})
    dispatch = queue.take(clock())
    assert dispatch is not None and dispatch.url == WORKER

    first, status = svc.dispatch("POST", "/", dispatch.body, "")
    assert status == 200 and first["outcome"] == "done"
    second, status = svc.dispatch("POST", "/", dispatch.body, "")
    assert status == 200 and second["outcome"] == "not mine"
    assert CALLS["search:sagas"] == 1, "the duplicate was paid for"


def test_a_timeout_with_nothing_due_writes_nothing():
    svc, store, _, clock = service()
    post(svc, "promise.create", id="p.1", timeoutAt=clock() + 1_000_000)
    before = store.get(doc_key("p"))

    assert svc.timeout(Timeout("p")) == ({"timeout": "p"}, 200)
    assert store.get(doc_key("p")) == before, "an idle timeout wrote a new generation"


def test_a_timeout_for_an_origin_that_has_never_existed_is_still_a_200():
    svc, store, _, _ = service()
    assert svc.timeout(Timeout("ghost"))[1] == 200
    assert store.objects == {}


# --- the whole thing, over nothing but HTTP --------------------------------


DONE = {"agent": 2, "search:durable execution": 1,
        "search:workflow recovery": 1, "search:sagas": 1}


def root(store, origin: str = ORIGIN):
    from resonate.codec import decode

    found = store.get(doc_key(origin))
    assert found, "nothing was ever written"
    return decode(found[0].encode()).get(origin).promise


def start(svc, clock, question: str = QUESTION) -> None:
    """How a client kicks off a run: an ordinary `promise.create` with a
    target. Nothing in the service knows it is the start of anything."""
    body, status = post(
        svc, "promise.create", id=ORIGIN, timeoutAt=clock() + 10 ** 9,
        param={"data": dumps({"f": "counted_research", "a": [question]}).data},
        tags={TAG_TARGET: WORKER})
    assert status == 200, body


def test_the_research_agent_runs_end_to_end_through_the_service():
    svc, store, queue, clock = service()
    start(svc, clock)
    settle(svc, queue, clock)
    settled = root(store)
    assert settled.state == "resolved", settled.state
    assert json.loads(settled.value.data) == EXPECTED
    assert dict(CALLS) == DONE


def test_it_still_runs_when_every_delivery_happens_twice():
    svc, store, queue, clock = service(duplicate=1.0, backoff=100)
    start(svc, clock)
    settle(svc, queue, clock, rounds=30)
    assert json.loads(root(store).value.data) == EXPECTED
    assert dict(CALLS) == DONE, "something was paid for twice"
    assert queue.delivered > 12, "the duplicates did not happen"


@pytest.mark.parametrize("seed", range(4))
def test_it_still_runs_over_a_queue_that_is_late_out_of_order_and_lossy(seed):
    svc, store, queue, clock = service(
        seed=seed, duplicate=0.4, shuffle=True, lateness=500, lose=0.3, backoff=100)
    start(svc, clock)
    for _ in range(40):
        while True:
            d = queue.take(clock())
            if d is None:
                break
            if queue.loses_this_one():
                queue.nack(d, clock())
                continue
            status = svc.dispatch("POST", "/", d.body, "")[1]
            assert status == 200
            queue.ack(d, clock())
        clock.advance(40_000)
    assert json.loads(root(store).value.data) == EXPECTED
    assert dict(CALLS) == DONE
