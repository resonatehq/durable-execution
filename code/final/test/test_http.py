"""The service as an HTTP service, through the entry point Cloud Run calls.

`test_app.py` stops one layer short: it calls `Routes.handle(method, path,
body, authorized)`, which is a Python method. Everything between an HTTP
request and that call — `handler`, `from_environment`, `verify`, the JSON
in and out, the status code, the path Flask hands over — was untested, and
it is exactly the layer a deployment gets wrong.

So this drives `app.handler` itself, through a real Flask app built the way
Cloud Run builds it:

    functions_framework.create_app("handler", "examples/research-agent/main.py")

That file is what a user writes: their functions, and `handler` re-exported
from the package in a single import. Driving an example rather than the
package is the point -- the import that makes the entry point exist is in
the example, so a test that loaded `resonate/app.py` directly would pass
with that line deleted.

`create_app` loads it as a module object of its own, which is why the
service has to be constructible from the environment rather than injected —
see `local.py`. `SIMULATED=1` gives it the in-memory store and queue and
changes nothing else: same engine, same kernel, same codec.

A Flask test client rather than a socket, on purpose. It builds a genuine
request object and runs the genuine view, so everything this file is about
is exercised, and a test suite that binds ports is a test suite that is
flaky on somebody's machine.

What is still not covered, and cannot be here: `id_token.verify_oauth2_token`
needs Google to answer. The refusal path above it — no token, wrong shape —
is covered, because that is decided before any network call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from resonate import local
from resonate.codec import decode, doc_key
from resonate.kernel import TAG_TARGET
from resonate.spec.queue import SWEEP
from exampleapp import path_to
from resonate.ports import Conflict, Unavailable
from resonate.sdk import dumps, route
from test_e2e import (
    CALLS, EXPECTED, ORIGIN, QUESTION, counted_agent, counted_research,
    counted_search,
)

#: Where this service answers. One service runs every function, which is the
#: smallest deployment that is still the real shape.
WORKER = "https://svc-abc.a.run.app/execute"

#: `create_app` resolves its source relative to the working directory, and
#: pytest's is wherever it was started from.
ROOT = Path(__file__).parent.parent


@pytest.fixture
def client(monkeypatch):
    """A real Flask app around the real handler, over the simulated ports."""
    import functions_framework

    monkeypatch.setenv("SIMULATED", "1")
    monkeypatch.setenv("ROUTES_WORKERS", json.dumps(
        {"research": WORKER, "agent": WORKER, "search": WORKER}))
    monkeypatch.delenv("ROUTES_ACCOUNT", raising=False)
    local.reset()
    CALLS.clear()
    # The example's `main.py`, not the package: that is the file the
    # platform loads, and the re-exported `handler` in it is the only wiring
    # a user writes. An entry point that worked when imported directly and
    # not through an example would be a broken deployment with a green suite.
    # A service built from *this* test's environment. `handler` caches one
    # per container, which is right in production and wrong across tests:
    # another module leaves that global set or cleared, and whichever test
    # ran first would decide what this one is talking to.
    from resonate.app import service

    service.cache_clear()
    app = functions_framework.create_app("handler", str(path_to("research-agent")))
    client = app.test_client()
    assert client.get("/ready").status_code == 200, "the service would not build"
    # Loading the example registers its own functions. These are different
    # ones -- `@resonate` refuses two registrations of a name, so they have
    # to be -- and they only need saying where they run.
    for fn in (counted_research, counted_agent, counted_search):
        route(fn, WORKER)
    return client


def post(client, kind, **data):
    return client.post("/", json={"kind": kind, "data": data})


# --- the routes, over HTTP -------------------------------------------------


def test_the_protocol_round_trips_over_http(client):
    r = client.post("/", json={"kind": "promise.create",
                               "data": {"id": "p.1", "timeoutAt": 10 ** 12}})
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("application/json")
    assert r.get_json()["data"]["promise"]["state"] == "pending"

    again = post(client, "promise.get", id="p.1")
    assert again.status_code == 200
    assert again.get_json()["data"]["promise"] == r.get_json()["data"]["promise"]


def test_the_status_the_kernel_chose_survives_the_wire(client):
    r = post(client, "promise.get", id="nothing")
    assert r.status_code == 404 and r.get_json()["head"]["status"] == 404


def test_a_malformed_body_is_a_400_not_a_500(client):
    assert client.post("/", json={"kind": "nonsense", "data": {}}).status_code == 400
    assert client.post("/", json={"kind": "promise.get"}).status_code == 400


def test_a_request_with_no_json_at_all_is_a_400(client):
    """`get_json(silent=True)` returns None rather than raising, and the
    handler has to survive that. A body-less POST is what a health checker
    or a stray probe sends."""
    r = client.post("/", data="", content_type="text/plain")
    assert r.status_code == 400, r.get_data(as_text=True)


def test_ready_answers_and_admits_it_is_simulated(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.get_json() == {"ready": True, "simulated": True}


def test_an_unknown_route_is_a_404_and_a_bad_method_a_405(client):
    assert client.post("/anything", json={}).status_code == 404
    assert client.get("/").status_code == 405


def test_the_queue_routes_refuse_an_unsigned_request(client, monkeypatch):
    """With a service account named, `verify` rejects anything without a
    bearer token before it ever calls Google — so the refusal is testable
    even though the acceptance is not."""
    monkeypatch.setenv("ROUTES_ACCOUNT", "worker@p.iam.gserviceaccount.com")
    assert client.post("/execute", json={}).status_code == 401
    assert client.post("/sweep/o", json={}).status_code == 401
    # The client route is not the queue's to sign.
    assert post(client, "promise.get", id="nothing").status_code == 404


# --- the whole counted_agent, over nothing but HTTP --------------------------------


def pump(client, budget: int = 2_000) -> int:
    """Cloud Tasks, as the only thing it is: a POST to a URL.

    This is the piece `test_app.py` could not have — the queue delivering
    over the same interface a real one would, into the same handler.
    """
    for did in range(budget):
        d = local.QUEUE.take(local.CLOCK())
        if d is None:
            return did
        path = "/" + d.url if d.url.startswith(SWEEP) else "/execute"
        r = client.post(path, json=d.body)
        assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True))
        local.QUEUE.ack(d, local.CLOCK())
    raise AssertionError("the queue never ran out of eligible work")


def settle(client, rounds: int = 12) -> None:
    for _ in range(rounds):
        pump(client)
        local.CLOCK.advance(40_000)
    pump(client)


def test_the_research_agent_runs_end_to_end_over_http(client):
    started = post(client, "promise.create", id=ORIGIN, timeoutAt=10 ** 12,
                   param={"data": dumps({"f": "counted_research", "a": [QUESTION]}).data},
                   tags={TAG_TARGET: WORKER})
    assert started.status_code == 200

    settle(client)

    found = local.STORE.get(doc_key(ORIGIN))
    assert found, "nothing was ever written"
    root = decode(found[0].encode(), ORIGIN).get(ORIGIN).promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == EXPECTED
    assert dict(CALLS) == {"agent": 2, "search:durable execution": 1,
                           "search:workflow recovery": 1, "search:sagas": 1}

    # And a client can read the answer back the same way it started it.
    r = post(client, "promise.get", id=ORIGIN)
    assert r.status_code == 200
    assert json.loads(r.get_json()["data"]["promise"]["value"]["data"]) == EXPECTED


# --- routing, and what a failure becomes over HTTP -------------------------
#
# These used to live in `test_app.py`, against a `Routes.handle(method,
# path, body)` that existed to be called that way. There is no such method
# now: a Cloud Run function has one entry point, so the routing is a ladder
# of `if`s in `handler` and the only honest way to exercise it is a real
# request. Which is also the better way -- the old tests could agree with a
# router that Flask never actually reached.


def test_an_unknown_route_is_a_404(client):
    assert client.post("/sweep", json={}).status_code == 404
    assert client.post("/anything", json={}).status_code == 404


def test_a_wrong_method_is_a_405(client):
    assert client.get("/").status_code == 405
    assert client.delete("/execute").status_code == 405


@pytest.mark.parametrize("path", ["/execute", "/sweep/o"])
def test_the_queue_s_routes_are_closed_to_anyone_it_did_not_sign_for(
        client, monkeypatch, path):
    """`verify` reads the account per request, so naming one here closes
    both routes without rebuilding the service."""
    monkeypatch.setenv("ROUTES_ACCOUNT", "queue@example.iam.gserviceaccount.com")
    assert client.post(path, json={}).status_code == 401
    assert local.STORE.objects == {}, "an unsigned request wrote something"


def test_the_client_route_is_not_the_queue_s_to_sign(client, monkeypatch):
    """`/` is fronted by whatever the deployment puts in front of it, not by
    an OIDC token from Cloud Tasks. A client carries no queue signature, and
    for a client that is normal."""
    monkeypatch.setenv("ROUTES_ACCOUNT", "queue@example.iam.gserviceaccount.com")
    answer = client.post("/", json={
        "kind": "promise.create",
        "data": {"id": "p.1", "timeoutAt": local.CLOCK() + 1_000}})
    assert answer.status_code == 200


def test_a_dispatch_that_is_not_a_message_is_a_400(client):
    assert client.post("/execute", json={"kind": "lunch"}).status_code == 400


class Refuses(local.STORE.__class__):
    """A bucket that answers every write the same way."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def put(self, key, body, **kw):
        raise self.error


@pytest.mark.parametrize("error,status", [
    (Unavailable("no answer"), 503),
    (Conflict("somebody else got there first"), 409),
])
def test_the_two_ways_a_bucket_refuses_are_two_statuses(client, error, status):
    """503: nothing is known about whether the write landed, so the queue
    retries and every operation is idempotent. 409: the decision was made
    against a document that no longer exists, so the caller must ask again
    and the kernel must decide again. One is a retry, the other is a
    re-decision, and a single status for both would lose that."""
    from resonate.app import service

    engine = service().engine
    was, engine.store = engine.store, Refuses(error)
    try:
        # An id of its own. `promise.create` is idempotent, so a create that
        # another test already made would change nothing, write nothing, and
        # never reach the bucket this one has broken.
        answer = client.post("/", json={
            "kind": "promise.create",
            "data": {"id": f"refused-{status}.1", "timeoutAt": local.CLOCK() + 1_000}})
    finally:
        engine.store = was
    assert answer.status_code == status, answer.get_json()
