from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import Unavailable

HORIZON_MS = 30 * 24 * 60 * 60 * 1_000


class Queue:
    def __init__(self, project: str, location: str, queue: str, base_url: str,
                 service_account: str | None = None, client: Any = None,
                 now: Any = None) -> None:
        if client is None:
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
            pass
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
