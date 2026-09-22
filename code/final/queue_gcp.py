"""A queue, for real: Google Cloud Tasks.

`queue_gcp.Queue` is `queues.QueueP` over a real queue. Two methods, because
that is all the engine needs: put a task in, take one out again. There is
no third method for receiving, and that is not an omission — Cloud Tasks is
push-only. A task is delivered by an HTTP POST to the URL it carries, which
is why `app.py` exists and why a worker is a service rather than a loop.

## What a task is here

    POST <base_url>/<url>          the handler, chosen by whoever created it
    body: the message, as JSON     what the kernel emitted
    schedule_time: not before      set for a deadline, absent for a dispatch

Only deadlines carry a schedule. A dispatch is never deferred, because
anything that must wait waits by having a deadline.

## Names come from the service

Never from us. A caller-chosen name leaves a tombstone for about an hour
after deletion, so re-creating the same name is refused — which is exactly
what a deadline re-armed at the same instant for the same origin would try
to do. Letting the service name it, and recording that name in the
document, avoids the trap and is why the document has a `tn` field.

## The horizon

Cloud Tasks will not schedule further out than 30 days. A promise with a
longer deadline is clamped, and the sweep it triggers finds nothing due and
re-arms. Cheap, and a place a bug would make a promise never time out, so
it has a test.

## What is not verified

This file has never been run against Google Cloud Tasks. It is written
against the documented API, and `queue_mem` is what the tests drive. The
two agree on the interface by construction and on the contract by
`queues.conformance`, which both pass, and on nothing else until someone
points this at a project.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ports import Unavailable

#: Cloud Tasks refuses a schedule further out than this.
HORIZON_MS = 30 * 24 * 60 * 60 * 1_000


class Queue:
    """The two operations over a real queue.

    `service_account` turns on OIDC: Cloud Tasks signs each request with a
    token for that account, and the handler verifies it. Without one the
    handler must be unreachable except from inside the network, and saying
    which of the two a deployment relies on is not optional.
    """

    def __init__(self, project: str, location: str, queue: str, base_url: str,
                 service_account: str | None = None, client: Any = None,
                 now: Any = None) -> None:
        if client is None:  # pragma: no cover - needs credentials
            from google.cloud import tasks_v2

            client = tasks_v2.CloudTasksClient()
        self.client = client
        self.parent = client.queue_path(project, location, queue)
        self.base_url = base_url.rstrip("/")
        self.service_account = service_account
        self.now = now or (lambda: int(datetime.now(timezone.utc).timestamp() * 1_000))

    def create(self, url: str, body: Any, *, not_before: int = 0) -> str:
        from google.api_core import exceptions as gcp
        from google.cloud import tasks_v2

        # A sweep is a path on this service; a worker is wherever it is,
        # which may be another service entirely. Both arrive here as the
        # address the kernel emitted, so both are honoured.
        target = url if url.startswith(("http://", "https://")) \
            else f"{self.base_url}/{url.lstrip('/')}"
        request: dict = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": target,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body).encode("utf-8"),
        }
        if self.service_account is not None:
            request["oidc_token"] = {"service_account_email": self.service_account}
        task: dict = {"http_request": request}
        if not_before:
            at = min(not_before, self.now() + HORIZON_MS)
            task["schedule_time"] = datetime.fromtimestamp(at / 1_000, tz=timezone.utc)
        try:
            created = self.client.create_task(parent=self.parent, task=task)
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
        return created.name

    def delete(self, name: str) -> None:
        from google.api_core import exceptions as gcp

        try:
            self.client.delete_task(name=name)
        except gcp.NotFound:
            # Already fired, already collected, or never there. All three are
            # the same to a caller that only wanted it gone.
            pass
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
