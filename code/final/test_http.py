"""The service as an HTTP service, through the entry point Cloud Run calls.

`test_app.py` stops one layer short: it calls `Service.handle(method, path,
body, authorized)`, which is a Python method. Everything between an HTTP
request and that call — `handler`, `from_environment`, `verify`, the JSON
in and out, the status code, the path Flask hands over — was untested, and
it is exactly the layer a deployment gets wrong.

So this drives `app.handler` itself, through a real Flask app built the way
Cloud Run builds it:

    functions_framework.create_app("handler", "app.py")

`create_app` loads `app.py` as a module object of its own, which is why the
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

import pytest

import local
from codec import decode, doc_key
from kernel import TAG_TARGET
from queues import SWEEP
from sdk import dumps, route
from test_e2e import CALLS, EXPECTED, ORIGIN, QUESTION, agent, research, search

#: Where this service answers. One service runs every function, which is the
#: smallest deployment that is still the real shape.
WORKER = "https://svc-abc.a.run.app/execute"


@pytest.fixture
def client(monkeypatch):
    """A real Flask app around the real handler, over the simulated ports."""
    import functions_framework

    monkeypatch.setenv("SIMULATED", "1")
    monkeypatch.setenv("WORKERS", json.dumps(
        {"research": WORKER, "agent": WORKER, "search": WORKER}))
    monkeypatch.delenv("SERVICE_ACCOUNT", raising=False)
    local.reset()
    CALLS.clear()
    for fn in (research, agent, search):
        route(fn, WORKER)
    return functions_framework.create_app("handler", "app.py").test_client()


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
    monkeypatch.setenv("SERVICE_ACCOUNT", "worker@p.iam.gserviceaccount.com")
    assert client.post("/execute", json={}).status_code == 401
    assert client.post("/sweep/o", json={}).status_code == 401
    # The client route is not the queue's to sign.
    assert post(client, "promise.get", id="nothing").status_code == 404


# --- the whole agent, over nothing but HTTP --------------------------------


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
                   param={"data": dumps({"f": "research", "a": [QUESTION]}).data},
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
