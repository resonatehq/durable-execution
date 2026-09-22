"""The service, driven the way Cloud Run drives it.

Nothing here calls the engine. Every test goes in through a method, a path
and a JSON body, and comes back a status code, because that is the only
surface production has. What the earlier suites prove about the kernel and
the engine is not re-proved; what is proved here is the thin, dull layer
that is nevertheless the one a deployment gets wrong: which route, which
code, and who is allowed to knock.

The one thing this file cannot reach is `from_environment` and `verify`,
which need credentials and a real request object. They are wiring, and they
are marked as such.
"""

from __future__ import annotations

import json

import pytest

from app import Service
from codec import doc_key
from kernel import KernelCfg, TAG_TARGET
from ports import Conflict, Unavailable
from runtime import Clock
from sdk import dumps, route
from store_mem import Store
from test_e2e import CALLS, EXPECTED, ORIGIN, QUESTION, agent, research, search
from queue_mem import Queue
from queues import SWEEP

CFG = KernelCfg(retry_timeout=30_000)

#: Where this service answers. One service runs every function here, which
#: is the smallest deployment that is still the real shape: the queue calls
#: back in over HTTP rather than handing anything to a loop.
WORKER = "https://svc-abc.a.run.app/execute"


def service(**knobs):
    """One container instance, one store, one queue."""
    CALLS.clear()
    store, queue, clock = Store(), Queue(**knobs), Clock()
    svc = Service(store, queue, CFG, pid="rev-1", ttl=60_000, clock=clock)
    for fn in (research, agent, search):
        route(fn, WORKER)
    return svc, store, queue, clock


def deliver(svc: Service, queue: Queue, clock: Clock, budget: int = 2_000) -> int:
    """Cloud Tasks, as the only thing it is: a POST to a URL.

    A delivery's url is either this service's `/execute` or the sweep path
    for an origin. Turning it back into a path is what the load balancer
    does in production and all it does.
    """
    for did in range(budget):
        d = queue.take(clock())
        if d is None:
            return did
        path = "/" + d.url if d.url.startswith(SWEEP) else "/execute"
        body, status = svc.handle("POST", path, d.body)
        assert status == 200, (path, status, body)
        queue.ack(d, clock())
    raise AssertionError("the queue never ran out of eligible work")


def settle(svc, queue, clock, rounds: int = 12) -> None:
    for _ in range(rounds):
        deliver(svc, queue, clock)
        clock.advance(40_000)
    deliver(svc, queue, clock)


def post(svc: Service, kind: str, **data):
    return svc.handle("POST", "/", {"kind": kind, "data": data})


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
    body, status = svc.handle("POST", "/", envelope)
    assert status == 400, body
    assert store.objects == {}, "a request that was never understood wrote something"


# --- the routing -----------------------------------------------------------


def test_an_unknown_route_is_a_404():
    svc, _, _, _ = service()
    assert svc.handle("POST", "/sweep", {})[1] == 404
    assert svc.handle("POST", "/anything", {})[1] == 404


def test_a_wrong_method_is_a_405():
    svc, _, _, _ = service()
    assert svc.handle("GET", "/", None)[1] == 405
    assert svc.handle("DELETE", "/execute", None)[1] == 405


@pytest.mark.parametrize("path", ["/execute", "/sweep/o"])
def test_the_queue_s_routes_are_closed_to_anyone_the_queue_did_not_sign_for(path):
    svc, store, _, _ = service()
    body, status = svc.handle("POST", path, {}, authorized=False)
    assert status == 401 and store.objects == {}


def test_the_client_route_is_not_the_queue_s_to_sign():
    """`/` is fronted by whatever the deployment puts in front of it, not by
    an OIDC token from Cloud Tasks. Passing `authorized=False` says the
    request carried no queue signature, which for a client is normal."""
    svc, _, _, clock = service()
    assert svc.handle("POST", "/", {
        "kind": "promise.create",
        "data": {"id": "p.1", "timeoutAt": clock() + 1_000}}, authorized=False)[1] == 200


def test_a_dispatch_that_is_not_a_message_is_a_400():
    svc, _, _, _ = service()
    assert svc.handle("POST", "/execute", {"kind": "lunch"})[1] == 400


# --- readiness -------------------------------------------------------------


class Unreachable(Store):
    """A bucket that has stopped answering."""

    def list(self, prefix, limit):
        raise Unavailable("the bucket did not answer")


def test_ready_says_whether_the_bucket_answers():
    svc, _, _, _ = service()
    assert svc.handle("GET", "/ready", None) == ({"ready": True}, 200)

    svc.store = Unreachable()
    body, status = svc.handle("GET", "/ready", None)
    assert status == 503 and body["ready"] is False


# --- what the two failures of a bucket mean over HTTP ----------------------


class Refuses(Store):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def put(self, key, body, **kw):
        raise self.error


def test_a_bucket_that_cannot_be_reached_is_a_503():
    """Nothing is known about whether the write landed. The queue retries,
    and every operation is idempotent, so retrying is safe."""
    svc, _, _, clock = service()
    svc.engine.store = Refuses(Unavailable("no answer"))
    assert post(svc, "promise.create", id="p.1", timeoutAt=clock() + 1_000)[1] == 503


def test_a_decision_the_state_moved_under_is_a_409():
    """Not a retry of the same write: the decision was made against a
    document that no longer exists, so the caller must ask again and the
    kernel must decide again."""
    svc, _, _, clock = service()
    svc.engine.store = Refuses(Conflict("somebody else got there first"))
    assert post(svc, "promise.create", id="p.1", timeoutAt=clock() + 1_000)[1] == 409


# --- the queue's routes ----------------------------------------------------


def test_a_duplicate_dispatch_is_answered_rather_than_retried():
    """At-least-once means the same `execute` arrives twice. The second
    finds the task claimed at a version it does not hold, and that refusal
    is a 200: delivering it a third time would not change anything."""
    svc, _, queue, clock = service()
    post(svc, "promise.create", id="w.1", timeoutAt=clock() + 1_000_000,
         param={"data": json.dumps({"f": "search", "a": ["sagas"]})},
         tags={TAG_TARGET: WORKER})
    dispatch = queue.take(clock())
    assert dispatch is not None and dispatch.url == WORKER

    first, status = svc.handle("POST", "/execute", dispatch.body)
    assert status == 200 and first["outcome"] == "done"
    second, status = svc.handle("POST", "/execute", dispatch.body)
    assert status == 200 and second["outcome"] == "not mine"
    assert CALLS["search:sagas"] == 1, "the duplicate was paid for"


def test_a_sweep_with_nothing_due_writes_nothing():
    svc, store, _, clock = service()
    post(svc, "promise.create", id="p.1", timeoutAt=clock() + 1_000_000)
    before = store.get(doc_key("p"))

    assert svc.handle("POST", "/sweep/p", None) == ({"swept": "p"}, 200)
    assert store.get(doc_key("p")) == before, "an idle sweep wrote a new generation"


def test_a_sweep_for_an_origin_that_has_never_existed_is_still_a_200():
    svc, store, _, _ = service()
    assert svc.handle("POST", "/sweep/ghost", None)[1] == 200
    assert store.objects == {}


# --- the whole thing, over nothing but HTTP --------------------------------


DONE = {"agent": 2, "search:durable execution": 1,
        "search:workflow recovery": 1, "search:sagas": 1}


def root(store, origin: str = ORIGIN):
    from codec import decode

    found = store.get(doc_key(origin))
    assert found, "nothing was ever written"
    return decode(found[0].encode(), origin).get(origin).promise


def start(svc, clock, question: str = QUESTION) -> None:
    """How a client kicks off a run: an ordinary `promise.create` with a
    target. Nothing in the service knows it is the start of anything."""
    body, status = post(
        svc, "promise.create", id=ORIGIN, timeoutAt=clock() + 10 ** 9,
        param={"data": dumps({"f": "research", "a": [question]}).data},
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
            path = "/" + d.url if d.url.startswith(SWEEP) else "/execute"
            assert svc.handle("POST", path, d.body)[1] == 200
            queue.ack(d, clock())
        clock.advance(40_000)
    assert json.loads(root(store).value.data) == EXPECTED
    assert dict(CALLS) == DONE
