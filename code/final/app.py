"""The service: three routes and nothing else.

Cloud Tasks is push-only, so a worker is not a loop, it is an endpoint.
That is the whole reason this file exists, and it is why the shape of the
deployment falls out of the queue rather than out of a preference:

    POST /                 a protocol request, from a client that does not
                           embed the engine
    POST /execute          a dispatch, delivered by the queue
    POST /sweep/<origin>   a deadline, delivered by the queue
    GET  /ready            whether the bucket answers

One instance builds one engine, at import, because that is once per
container rather than once per request. Everything it needs comes from the
environment, and nothing in this file decides policy: which bucket, which
queue, which account, and where the other workers live are all deployment.

## Authentication

Cloud Tasks signs each delivery with an OIDC token for the service account
it was told to use, and `/execute` and `/sweep` verify it. `/` is a client
endpoint and is not signed by anything here, so it must be protected by
whatever fronts the service. A deployment that leaves `SERVICE_ACCOUNT`
unset is saying the service is unreachable except from inside its network,
and had better mean it.

## What is not verified

This file has never run on Cloud Run. The routing, the parsing and the
error mapping are exercised by `test_app.py` against a fake request; the
rest is the documented behaviour of three libraries.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from blob import BlobStore
from cloudtasks import CloudTasksQueue
from engine import Timeout
from gcs import GcsBlob
from kernel import KernelCfg
from ports import Conflict, Unavailable
from runtime import Clock, Worker
from sdk import route
from tasks import QueueTimers, QueueTransport
from wire import Invalid, decode_message, encode_reply, parse_request


def wall_clock() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


class Service:
    """Everything one container instance needs, built once."""

    def __init__(self, blob, queue, cfg: KernelCfg, pid: str, ttl: int, clock=wall_clock) -> None:
        from engine import Engine

        self.clock = clock
        self.queue = queue
        self.engine = Engine(BlobStore(blob), QueueTimers(queue), QueueTransport(queue), cfg)
        self.worker = Worker(self.engine, Clock(), pid, ttl)
        # The worker's clock is the wall clock, not a test's.
        self.worker.clock = clock
        self.blob = blob

    # -- the routes --------------------------------------------------------

    def protocol(self, envelope: dict) -> tuple[dict, int]:
        try:
            request = parse_request(envelope)
        except Invalid as e:
            return {"head": {"status": 400}, "data": str(e)}, 400
        reply = self.engine.process(request, self.clock())
        return encode_reply(reply), 200 if reply.status < 400 else reply.status

    def execute(self, body: dict) -> tuple[dict, int]:
        """A dispatch. A refused acquire is still a 2xx: somebody else has
        the task, and delivering this again would not change that."""
        message = decode_message(body)
        outcome = self.worker.execute(message.task_id, message.version)
        return {"outcome": outcome}, 200

    def sweep(self, origin: str) -> tuple[dict, int]:
        """A deadline. Idempotent, so a duplicate finds nothing due and
        writes nothing."""
        self.engine.process(Timeout(origin), self.clock())
        return {"swept": origin}, 200

    def ready(self) -> tuple[dict, int]:
        try:
            self.blob.list("", 1)
        except Unavailable as e:
            return {"ready": False, "why": str(e)}, 503
        return {"ready": True}, 200

    # -- what the routes have in common ------------------------------------

    def handle(self, method: str, path: str, body: dict | None,
               authorized: bool = True) -> tuple[dict, int]:
        if method == "GET" and path == "/ready":
            return self.ready()
        if method != "POST":
            return {"error": "POST"}, 405
        if path.startswith("/sweep/") or path == "/execute":
            if not authorized:
                return {"error": "unauthenticated"}, 401
        try:
            if path == "/":
                return self.protocol(body or {})
            if path == "/execute":
                return self.execute(body or {})
            if path.startswith("/sweep/"):
                return self.sweep(path[len("/sweep/"):])
        except Invalid as e:
            return {"error": str(e)}, 400
        except Conflict as e:
            # The state moved under this decision. Nothing was written, and
            # the caller — a client, or the queue — retries.
            return {"error": str(e)}, 409
        except Unavailable as e:
            # Nothing is known about whether it landed. The queue will try
            # again; every operation is idempotent.
            return {"error": str(e)}, 503
        return {"error": "no such route"}, 404


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def from_environment() -> Service:  # pragma: no cover - needs credentials
    """What a container is told at boot.

    `WORKERS` routes function names to the URLs they run at, as JSON, which
    is the only thing in this system that knows the shape of the
    deployment: `{"search": "https://search-abc.a.run.app/execute"}`.
    """
    blob = GcsBlob(os.environ["BUCKET"])
    queue = CloudTasksQueue(
        project=os.environ["PROJECT"],
        location=os.environ["LOCATION"],
        queue=os.environ["QUEUE"],
        base_url=os.environ["BASE_URL"],
        service_account=os.environ.get("SERVICE_ACCOUNT"),
    )
    for name, url in json.loads(os.environ.get("WORKERS", "{}")).items():
        from sdk import TARGETS

        TARGETS[name] = url
    return Service(
        blob, queue,
        KernelCfg(retry_timeout=int(os.environ.get("RETRY_TIMEOUT", 30_000))),
        pid=os.environ.get("K_REVISION", "local"),
        ttl=int(os.environ.get("LEASE", 60_000)),
    )


def verify(request) -> bool:  # pragma: no cover - needs credentials
    """Whether Cloud Tasks signed this. Unset means the deployment is
    relying on the network instead, which is a choice it has to make out
    loud."""
    account = os.environ.get("SERVICE_ACCOUNT")
    if account is None:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            header[len("Bearer "):], grequests.Request(),
            audience=os.environ.get("AUDIENCE"))
    except ValueError:
        return False
    return claims.get("email") == account and claims.get("email_verified", False)


SERVICE: Service | None = None


def handler(request):  # pragma: no cover - needs functions_framework
    """The entry point. `gcloud run deploy --function handler`."""
    global SERVICE
    if SERVICE is None:
        SERVICE = from_environment()
    body, status = SERVICE.handle(
        request.method, request.path, request.get_json(silent=True), verify(request))
    return json.dumps(body), status, {"Content-Type": "application/json"}
