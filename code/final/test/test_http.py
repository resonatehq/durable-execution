"""The service as an HTTP service, through the entry point Cloud Run calls.

`test_app.py` stops one layer short: it calls `Server.protocol`,
`Server.execute` and `Server.sweep` directly. Everything between an HTTP
request and those calls — `handler`, `Server.dispatch`, `Server.authorized`,
the JSON in and out, the status code, the path Flask hands over — is what
this file covers, and it is exactly the layer a deployment gets wrong.

So this drives the `handler` that `serve()` returns, through a real Flask
app built the way Cloud Run builds it:

    functions_framework.create_app("handler", "examples/research-agent/main.py")

That file is what a user writes: their functions, and `handler = serve()`
on the last line. Driving an example rather than the package is the point
-- the line that makes the entry point exist is in the example.

`create_app` loads it as a module object of its own and registers it in
`sys.modules` as `main`, which is how a test reaches the `Server` behind
the handler. `SIMULATED=1` gives it the in-memory store, queue and clock
and changes nothing else: same engine, same kernel, same codec.

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
import sys
from pathlib import Path

import pytest

from resonate import store_mem
from resonate.codec import decode, doc_key
from resonate.kernel import TAG_TARGET
from resonate.types import SWEEP
from exampleapp import path_to
from resonate.errors import Conflict, Unavailable
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
def build(monkeypatch):
    """A real Flask app around the real handler, over the simulated ports.

    Each call loads the example afresh, so each gets a service built from
    the environment as it is at that moment."""
    import functions_framework

    def build(**env):
        monkeypatch.setenv("SIMULATED", "1")
        monkeypatch.setenv("ROUTES_WORKERS", json.dumps(
            {"research": WORKER, "agent": WORKER, "search": WORKER}))
        monkeypatch.delenv("ROUTES_ACCOUNT", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        CALLS.clear()
        # The example's `main.py`, not the package: that is the file the
        # platform loads, and `handler = serve()` in it is the only wiring a
        # user writes.
        app = functions_framework.create_app("handler", str(path_to("research-agent")))
        server = sys.modules["main"].handler.server
        client = app.test_client()
        # Loading the example registers its own functions. These are different
        # ones -- `@resonate` refuses two registrations of a name, so they have
        # to be -- and they only need saying where they run.
        for fn in (counted_research, counted_agent, counted_search):
            route(fn, WORKER)
        return client, server

    yield build
    sys.modules.pop("main", None)


@pytest.fixture
def served(build):
    return build()


@pytest.fixture
def client(served):
    return served[0]


@pytest.fixture
def server(served):
    return served[1]


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


def test_an_unknown_route_is_a_404_and_a_bad_method_a_405(client):
    assert client.post("/anything", json={}).status_code == 404
    assert client.get("/").status_code == 405


def test_the_queue_routes_refuse_an_unsigned_request(build):
    """With a service account named, `authorized` rejects anything without a
    bearer token before it ever calls Google — so the refusal is testable
    even though the acceptance is not."""
    client, _ = build(ROUTES_ACCOUNT="worker@p.iam.gserviceaccount.com")
    assert client.post("/execute", json={}).status_code == 401
    assert client.post("/sweep/o", json={}).status_code == 401
    # The client route is not the queue's to sign.
    assert post(client, "promise.get", id="nothing").status_code == 404


# --- the whole counted_agent, over nothing but HTTP --------------------------------


def pump(client, server, budget: int = 2_000) -> int:
    """Cloud Tasks, as the only thing it is: a POST to a URL.

    This is the piece `test_app.py` could not have — the queue delivering
    over the same interface a real one would, into the same handler.
    """
    queue, clock = server.engine.queue, server.clock
    for did in range(budget):
        d = queue.take(clock())
        if d is None:
            return did
        path = "/" + d.url if d.url.startswith(SWEEP) else "/execute"
        r = client.post(path, json=d.body)
        assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True))
        queue.ack(d, clock())
    raise AssertionError("the queue never ran out of eligible work")


def settle(client, server, rounds: int = 12) -> None:
    for _ in range(rounds):
        pump(client, server)
        server.clock.advance(40_000)
    pump(client, server)


def test_the_research_agent_runs_end_to_end_over_http(client, server):
    started = post(client, "promise.create", id=ORIGIN, timeoutAt=10 ** 12,
                   param={"data": dumps({"f": "counted_research", "a": [QUESTION]}).data},
                   tags={TAG_TARGET: WORKER})
    assert started.status_code == 200

    settle(client, server)

    found = server.engine.store.get(doc_key(ORIGIN))
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
# Through a real request rather than `Server.dispatch`, so the tests cannot
# agree with a router that Flask never actually reached.


def test_an_unknown_route_is_a_404(client):
    assert client.post("/sweep", json={}).status_code == 404
    assert client.post("/anything", json={}).status_code == 404


def test_a_wrong_method_is_a_405(client):
    assert client.get("/").status_code == 405
    assert client.delete("/execute").status_code == 405


@pytest.mark.parametrize("path", ["/execute", "/sweep/o"])
def test_the_queue_s_routes_are_closed_to_anyone_it_did_not_sign_for(build, path):
    client, server = build(ROUTES_ACCOUNT="queue@example.iam.gserviceaccount.com")
    assert client.post(path, json={}).status_code == 401
    assert server.engine.store.objects == {}, "an unsigned request wrote something"


def test_the_client_route_is_not_the_queue_s_to_sign(build):
    """`/` is fronted by whatever the deployment puts in front of it, not by
    an OIDC token from Cloud Tasks. A client carries no queue signature, and
    for a client that is normal."""
    client, server = build(ROUTES_ACCOUNT="queue@example.iam.gserviceaccount.com")
    answer = client.post("/", json={
        "kind": "promise.create",
        "data": {"id": "p.1", "timeoutAt": server.clock() + 1_000}})
    assert answer.status_code == 200


def test_a_dispatch_that_is_not_a_message_is_a_400(client):
    assert client.post("/execute", json={"kind": "lunch"}).status_code == 400


class Refuses(store_mem.Store):
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
def test_the_two_ways_a_bucket_refuses_are_two_statuses(client, server, error, status):
    """503: nothing is known about whether the write landed, so the queue
    retries and every operation is idempotent. 409: the decision was made
    against a document that no longer exists, so the caller must ask again
    and the kernel must decide again. One is a retry, the other is a
    re-decision, and a single status for both would lose that."""
    server.engine.store = Refuses(error)
    answer = client.post("/", json={
        "kind": "promise.create",
        "data": {"id": f"refused-{status}.1", "timeoutAt": server.clock() + 1_000}})
    assert answer.status_code == status, answer.get_json()
