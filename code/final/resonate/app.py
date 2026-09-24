"""The service: four routes and nothing else.

Cloud Tasks is push-only, so a worker is not a loop, it is an endpoint.
That is the whole reason this file exists, and it is why the shape of the
deployment falls out of the queue rather than out of a preference:

    POST /                 a protocol request, from a client that does not
                           embed the engine
    POST /execute          a dispatch, delivered by the queue
    POST /sweep/<origin>   a deadline, delivered by the queue
    GET  /ready            whether the bucket answers

One instance builds one engine, on the first request and not at import,
because that is once per container rather than once per request -- see
`service()`. Everything it needs comes from the
environment, and nothing in this file decides policy: which bucket, which
queue, which account, and where the other workers live are all deployment.

## Authentication

Cloud Tasks signs each delivery with an OIDC token for the service account
it was told to use, and `/execute` and `/sweep` verify it. `/` is a client
endpoint and is not signed by anything here, so it must be protected by
whatever fronts the service. A deployment that leaves `ROUTES_ACCOUNT`
unset is saying the service is unreachable except from inside its network,
and had better mean it.

## Running it

    SIMULATED=1 functions-framework --target=handler

serves the whole protocol on localhost over the in-memory ports — same
engine, same kernel, same codec. An example's `main.py` is the entry point
the buildpack insists on; it imports `handler` from here.

## Where the routing is

In `handler`, at the bottom, as one function and a ladder of `if`s. A Cloud
Run function has exactly one entry point -- the framework hands you a Flask
request and there are no route decorators to hang four paths off -- so any
router is one you write, and writing it anywhere else means a reader has to
go and find it. `Routes` holds what each route *does*, in methods that take
plain values, which is what lets a test drive a whole research agent
without a request object.

## What is verified

This has run on Cloud Run. On 2026-09-23 the research agent ran to
completion there: a client task created the promise, Cloud Tasks dispatched
each step with an OIDC token Google minted, every transition committed to
Google Cloud Storage under a generation precondition, and the run resolved
to the answer the simulator gives, in 22 conditional writes across six
promises. The deadline was armed and disarmed as designed -- the finished
document carries no `ta`, so the run left nothing queued -- and the split
the local trace shows held up: the two awaited calls ran in-process and the
three `rpc` branches went over the queue.

What that took, besides the code: eleven IAM bindings and ten distinct
permission failures, none of which the documentation implies. Three are
worth repeating because they are not guessable. Creating a task with an
OIDC token needs `actAs` on the account signed for, which a service does
not get by running as it. Retiring a deadline needs `cloudtasks.taskDeleter`,
which `enqueuer` does not include, and without it every transition
livelocks: the sweep re-arms, the disarm 403s, and the queue retries. And
`roles/storage.objectAdmin` does not include `storage.buckets.get`, which
this code never needs and the build tooling does.

## What is still not verified

The recovery path on the deployment. A dispatch thrown away unexecuted was
recovered by the armed deadline over a local socket, and the deadline is
armed and disarmed correctly here, but no run on Cloud Run has yet lost a
message and been rescued by a sweep.

Concurrency, too: every run so far has been one at a time. What several
instances do to one origin's object under real contention is measured in
`store_gcp.py` and unobserved here.

And the local layers stay what they were: `test_app.py` drives the router
directly, `test_http.py` drives this file's `handler` through a real Flask
app built the way Cloud Run builds it, and on 2026-09-22 the same entry
point ran over a bound socket with Google Cloud Storage underneath -- where
a dropped dispatch was recovered by its deadline and every unit of work was
still done exactly once. Those remain the cheap checks; the deployment is
the expensive one.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone

from . import queue_gcp
from . import store_gcp
from .engine import Timeout
from .kernel import KernelCfg
from .ports import Conflict, Unavailable
from .runtime import Clock, Worker
import flask
import functions_framework

from .tracing import because, trace
from .types import Invalid, decode_message, encode_reply, parse_request


def wall_clock() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


class Routes:
    """The four routes, and everything one container needs to serve them.

    Two jobs in one object, on purpose. It is the **composition root**:
    `__init__` builds the engine and the worker, once per container rather
    than once per request, out of whatever the environment named. And it
    is the **router**: method and path in, body and status out.

    What it is not is the HTTP layer. It takes plain values and returns
    plain values — no request object, no headers, no socket, no
    serialisation. That is `handler`, ten lines at the bottom of this
    file, and the separation is what lets `test_app.py` drive every route
    and a whole research agent without Flask.

    What it is not is the router. Which path reaches which of these is in
    `handler` at the bottom of this file, in one function, in the order the
    checks happen -- because that is the question a reader arrives with and
    it should not take three files to answer.

    Each route does speak HTTP, though, which is worth admitting: a status
    comes back with every body. The mapping from *failures* to statuses is
    `handler`'s, because a `Conflict` is not something a route decides.
    """

    def __init__(self, store, queue, cfg: KernelCfg, pid: str, ttl: int,
                 clock=wall_clock) -> None:
        from .engine import Engine

        self.clock = clock
        self.store, self.queue = store, queue
        self.engine = Engine(store, queue, cfg)
        self.worker = Worker(self.engine, Clock(), pid, ttl)
        # The worker's clock is the wall clock, not a test's.
        self.worker.clock = clock

    # -- the routes --------------------------------------------------------

    @trace
    def protocol(self, envelope: dict) -> tuple[dict, int]:
        with because("POST /"):
            return self._protocol(envelope)

    def _protocol(self, envelope: dict) -> tuple[dict, int]:
        try:
            request = parse_request(envelope)
        except Invalid as e:
            return {"head": {"status": 400}, "data": str(e)}, 400
        reply = self.engine.process(request, self.clock())
        return encode_reply(reply), 200 if reply.status < 400 else reply.status

    @trace
    def execute(self, body: dict) -> tuple[dict, int]:
        """A dispatch. A refused acquire is still a 2xx: somebody else has
        the task, and delivering this again would not change that."""
        with because("POST /execute"):
            return self._execute(body)

    def _execute(self, body: dict) -> tuple[dict, int]:
        message = decode_message(body)
        outcome = self.worker.execute_until_blocked_outer(
            message.task_id, message.version)
        return {"outcome": outcome}, 200

    @trace
    def sweep(self, origin: str) -> tuple[dict, int]:
        """A deadline. Idempotent, so a duplicate finds nothing due and
        writes nothing."""
        with because(f"POST /sweep/{origin}"):
            return self._sweep(origin)

    def _sweep(self, origin: str) -> tuple[dict, int]:
        self.engine.process(Timeout(origin), self.clock())
        return {"swept": origin}, 200

    @trace
    def ready(self) -> tuple[dict, int]:
        try:
            self.store.list("", 1)
        except Unavailable as e:
            return {"ready": False, "why": str(e)}, 503
        return {"ready": True, "simulated": bool(os.environ.get("SIMULATED"))}, 200

# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def from_environment() -> Routes:
    """What a container is told at boot.

    `SIMULATED=1` swaps the two ports for the in-memory ones and nothing
    else: the same engine, the same kernel, the same codec, over a store
    and a queue that forget everything when the process stops. It is how
    the service runs on a laptop and how `test_http.py` drives the real
    `handler`. `/ready` reports it, because a deployment that set it by
    accident would look healthy while losing every run.
    """
    _trace()
    if os.environ.get("SIMULATED"):
        from . import local

        _route()
        return Routes(local.STORE, local.QUEUE, _cfg(),
                       pid=os.environ.get("K_REVISION", "local"),
                       ttl=int(os.environ.get("LEASE", 60_000)),
                       clock=local.CLOCK)
    return _from_gcp()


def _trace() -> None:
    """`TRACE=1` sends spans to Cloud Trace, and nothing else changes.

    Off is the default and off is free: `otel.py`'s hooks are one lookup
    and a return, and the engine, the worker and the SDK behave identically
    either way. That is why this is an environment variable rather than a
    build: turning it on during an incident should not require a deploy.

    A tracer that cannot start is logged and dropped rather than raised.
    Refusing to boot because the observability is unavailable would mean
    the observability can take the service down, which is the one thing it
    must never do.
    """
    if not os.environ.get("TRACE"):
        return
    try:
        from . import otel_gcp

        otel_gcp.install()
    except Exception as e:  # pragma: no cover - needs a broken environment
        logging.getLogger(__name__).warning("tracing is off: %s", e)


def _cfg() -> KernelCfg:
    return KernelCfg(retry_timeout=int(os.environ.get("RETRY_TIMEOUT", 30_000)))


def _route() -> None:
    """`ROUTES_WORKERS` routes function names to the URLs they run at, as
    JSON. The only thing in this system that knows the shape of the
    deployment: `{"search": "https://search-abc.a.run.app/execute"}`.

    Not `WORKERS`, which is what this was called until Cloud Run refused to
    start it: `functions-framework` reads `WORKERS` as gunicorn's worker
    count and dies on `int()` of our JSON, before the container ever listens
    on its port. Nothing local sees it -- `test_http.py` builds the app with
    `create_app` and never starts gunicorn -- so the name has to stay out of
    the runtime's namespace rather than be tested into safety."""
    from importlib import import_module

    from .sdk import REGISTRY, TARGETS

    # `ROUTES_APP` names the modules whose `@resonate` functions this worker
    # can run, comma separated. Importing them is what fills `sdk.REGISTRY`,
    # and a worker with an empty registry answers `KeyError` to the first
    # dispatch it is handed -- it serves the protocol perfectly and executes
    # nothing. The first Cloud Run deployment did exactly that, because the
    # only module defining the example functions was a test file, which the
    # build does not ship.
    for module in os.environ.get("ROUTES_APP", "").split(","):
        if module.strip():
            import_module(module.strip())

    # Every function runs here unless told otherwise. One service running
    # everything is the common deployment and the one a user starts with,
    # and in that shape the routing table is derivable: `.rpc` on any
    # registered function is a dispatch to this service's own `/execute`.
    # Without this default a single-service deployment still had to hand-
    # write a JSON map of every function to the one URL it already knew,
    # and a typo in it surfaced as `KeyError` at the first dispatch rather
    # than at deploy.
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if base:
        # By name, not by name and version: a version is a generation of
        # code, and every generation of a function runs wherever that
        # function runs. Two of them in different services would be two
        # deployments of one name, which is the thing versions exist to
        # avoid needing.
        for name, _version in REGISTRY:
            TARGETS.setdefault(name, f"{base}/execute")

    # Explicit second, so it overrides. This is the split deployment: some
    # functions live in another service, and naming one here says so
    # without saying anything about the rest.
    for name, url in json.loads(os.environ.get("ROUTES_WORKERS", "{}")).items():
        TARGETS[name] = url


def _from_gcp() -> Routes:  # pragma: no cover - needs credentials
    store = store_gcp.Store(os.environ["BUCKET"])
    queue = queue_gcp.Queue(
        project=os.environ["PROJECT"],
        location=os.environ["LOCATION"],
        queue=os.environ["QUEUE"],
        base_url=os.environ["BASE_URL"],
        service_account=os.environ.get("ROUTES_ACCOUNT"),
    )
    _route()
    return Routes(
        store, queue, _cfg(),
        pid=os.environ.get("K_REVISION", "local"),
        ttl=int(os.environ.get("LEASE", 60_000)),
    )


def verify(request) -> bool:
    """Whether Cloud Tasks signed this. Unset means the deployment is
    relying on the network instead, which is a choice it has to make out
    loud."""
    account = os.environ.get("ROUTES_ACCOUNT")
    if account is None:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    # Everything below needs Google to answer, so it is where the tested
    # part stops: an unsigned request is refused above, without a network.
    from google.auth.transport import requests as grequests  # pragma: no cover
    from google.oauth2 import id_token  # pragma: no cover

    try:  # pragma: no cover
        claims = id_token.verify_oauth2_token(
            header[len("Bearer "):], grequests.Request(),
            audience=os.environ.get("AUDIENCE"))
    except ValueError:
        return False
    return claims.get("email") == account and claims.get("email_verified", False)


@functools.cache
def service() -> Routes:
    """Built once per container, on the first request rather than at import.

    Lazily because an example's `main.py` is both the user's functions and
    the entry point, and importing it to get at a function -- a test does,
    another worker's `ROUTES_APP` does -- must not require a bucket.
    """
    return from_environment()


@functions_framework.http
def handler(request):
    """Every route this service has, in the order they are checked.

    One function and a ladder of `if`s, because that is what a Cloud Run
    function is: the framework hands you *one* entry point and a Flask
    request, and there are no route decorators to hang four paths off. Any
    router is therefore one you write, and writing it anywhere but here
    means a reader has to find it.

    The four cases below are the whole of the service's surface. What each
    one does is a method on `Routes`, which takes plain values and knows
    nothing about HTTP, so a test can drive a whole research agent without
    a request object.
    """
    routes, method, path = service(), request.method, request.path
    if method == "GET" and path == "/ready":
        return answer(*routes.ready())
    if method != "POST":
        return answer({"error": "POST"}, 405)

    # The queue's two routes, and only they, must carry the OIDC token
    # Cloud Tasks signed. `/` is a client endpoint: whatever fronts this
    # service protects it, and `ROUTES_ACCOUNT` being unset says so out
    # loud.
    if (path == "/execute" or path.startswith("/sweep/")) and not verify(request):
        return answer({"error": "unauthenticated"}, 401)

    body = request.get_json(silent=True) or {}
    try:
        if path == "/":
            return answer(*routes.protocol(body))
        if path == "/execute":
            return answer(*routes.execute(body))
        if path.startswith("/sweep/"):
            return answer(*routes.sweep(path[len("/sweep/"):]))
    except Invalid as e:
        # Never a request. Nothing was read and nothing written.
        return answer({"error": str(e)}, 400)
    except Conflict as e:
        # The state moved under this decision. Nothing was written, and
        # the caller -- a client, or the queue -- retries.
        return answer({"error": str(e)}, 409)
    except Unavailable as e:
        # Nothing is known about whether it landed. The queue will try
        # again; every operation is idempotent.
        return answer({"error": str(e)}, 503)
    return answer({"error": "no such route"}, 404)


def answer(body: dict, status: int):
    """A route's plain values, as an HTTP response."""
    return flask.jsonify(body), status
