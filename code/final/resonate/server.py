"""The HTTP service: four routes over one engine and one worker.

    POST /                the protocol, for clients
    POST /execute         a task dispatched by the queue
    POST /sweep/<origin>  a deadline fired by the queue
    GET  /ready           whether the bucket answers

`config.py` builds a `Server` from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import flask

from .engine import Engine, Timeout
from .ports import Conflict, Unavailable
from .runtime import Worker
from .tracing import because, trace
from .types import Invalid, decode_message, encode_reply, parse_request


@dataclass
class Server:
    engine: Engine
    worker: Worker
    clock: Callable[[], int]
    account: str | None = None      # the queue's service account; None turns auth off
    audience: str | None = None
    simulated: bool = False

    def handle(self, request: flask.Request):
        """A Flask request in, a Flask response out."""
        try:
            body, status = self.dispatch(
                request.method, request.path,
                request.get_json(silent=True) or {},
                request.headers.get("Authorization", ""))
        except Invalid as e:
            body, status = {"error": str(e)}, 400
        except Conflict as e:
            body, status = {"error": str(e)}, 409
        except Unavailable as e:
            body, status = {"error": str(e)}, 503
        return flask.jsonify(body), status

    def dispatch(self, method: str, path: str, body: dict,
                 authorization: str) -> tuple[dict, int]:
        if method == "GET" and path == "/ready":
            return self.ready()
        if method != "POST":
            return {"error": "POST"}, 405
        if path == "/":
            return self.protocol(body)
        if not self.authorized(authorization):
            return {"error": "unauthenticated"}, 401
        if path == "/execute":
            return self.execute(body)
        if path.startswith("/sweep/"):
            return self.sweep(path.removeprefix("/sweep/"))
        return {"error": "no such route"}, 404

    @trace
    def protocol(self, envelope: dict) -> tuple[dict, int]:
        with because("POST /"):
            try:
                request = parse_request(envelope)
            except Invalid as e:
                return {"head": {"status": 400}, "data": str(e)}, 400
            reply = self.engine.process(request, self.clock())
            return encode_reply(reply), 200 if reply.status < 400 else reply.status

    @trace
    def execute(self, body: dict) -> tuple[dict, int]:
        with because("POST /execute"):
            message = decode_message(body)
            outcome = self.worker.execute_until_blocked_outer(
                message.task_id, message.version)
            return {"outcome": outcome}, 200

    @trace
    def sweep(self, origin: str) -> tuple[dict, int]:
        with because(f"POST /sweep/{origin}"):
            self.engine.process(Timeout(origin), self.clock())
            return {"swept": origin}, 200

    @trace
    def ready(self) -> tuple[dict, int]:
        try:
            self.engine.store.list("", 1)
        except Unavailable as e:
            return {"ready": False, "why": str(e)}, 503
        return {"ready": True, "simulated": self.simulated}, 200

    def authorized(self, authorization: str) -> bool:
        """Whether the request carries an OIDC token for `account`."""
        if self.account is None:
            return True
        if not authorization.startswith("Bearer "):
            return False
        from google.auth.transport import requests  # pragma: no cover
        from google.oauth2 import id_token  # pragma: no cover

        try:  # pragma: no cover
            claims = id_token.verify_oauth2_token(
                authorization.removeprefix("Bearer "), requests.Request(),
                audience=self.audience)
        except ValueError:
            return False
        return claims.get("email") == self.account and claims.get("email_verified", False)
