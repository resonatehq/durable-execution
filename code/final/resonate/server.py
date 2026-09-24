"""The HTTP service: one route over one engine and one worker.

    POST /   the body's `kind` says what it is:
               execute   a task dispatched by the queue
               timeout   a deadline fired by the queue
               anything else is a protocol request from a client

`config.py` builds a `Server` from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import flask

from .engine import Engine
from .errors import Conflict, Unavailable
from .types import (
    Execute, Invalid, Timeout, decode_message, encode_reply, parse_request,
)
from .worker import Worker


@dataclass
class Server:
    engine: Engine
    worker: Worker
    clock: Callable[[], int]
    account: str | None = None      # the queue's service account; None turns auth off
    audience: str | None = None

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
        if method != "POST":
            return {"error": "POST"}, 405
        if path != "/":
            return {"error": "no such route"}, 404
        match body.get("kind"):
            case "execute" | "timeout" if not self.authorized(authorization):
                return {"error": "unauthenticated"}, 401
            case "execute":
                return self.execute(decode_message(body))
            case "timeout":
                return self.timeout(decode_message(body))
            case _:
                return self.protocol(body)

    def protocol(self, envelope: dict) -> tuple[dict, int]:
        try:
            request = parse_request(envelope)
        except Invalid as e:
            return {"head": {"status": 400}, "data": str(e)}, 400
        reply = self.engine.process(request, self.clock())
        return encode_reply(reply), 200 if reply.status < 400 else reply.status

    def execute(self, message: Execute) -> tuple[dict, int]:
        outcome = self.worker.run(message.task_id, message.version)
        return {"outcome": outcome}, 200

    def timeout(self, message: Timeout) -> tuple[dict, int]:
        self.engine.process(message, self.clock())
        return {"timeout": message.origin}, 200

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
