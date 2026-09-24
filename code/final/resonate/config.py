"""Build the service from the environment.

    handler = serve()

at the bottom of a user's `main.py`, after their `@resonate` functions.

    SIMULATED        in-memory store, queue and clock instead of GCP
    BUCKET           the Cloud Storage bucket holding the documents
    PROJECT          \\
    LOCATION          > the Cloud Tasks queue
    QUEUE            /
    BASE_URL         where this service answers; every function runs here
                     unless ROUTES_WORKERS says otherwise
    ROUTES_WORKERS   JSON {function name: worker url}, for a split deployment
    ROUTES_ACCOUNT   the service account the queue signs with; unset turns
                     auth off for execute and timeout messages
    AUDIENCE         the audience that token is checked against
    RETRY_TIMEOUT    ms a claimed task may go quiet before it is offered again
    LEASE            ms a worker holds a task
    K_REVISION       this worker's id (set by Cloud Run)
    K_SERVICE        this service's name in traces (set by Cloud Run)
    TRACE            export spans to Cloud Trace
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Mapping

from . import queue_gcp, queue_mem, store_gcp, store_mem
from .engine import Engine
from .kernel import KernelCfg
from .testing.sim import Clock
from .worker import Worker
from .sdk import REGISTRY, TARGETS
from .server import Server


def serve(env: Mapping[str, str] = os.environ):
    """The entry point for `functions-framework --target handler`."""
    server = build(env)

    # functions-framework only accepts a plain function, not a bound method.
    def handler(request):
        return server.handle(request)

    handler.server = server
    return handler


def build(env: Mapping[str, str]) -> Server:
    if env.get("TRACE"):
        install_tracing(env)
    store, queue, clock = backends(env)
    register_targets(env)
    engine = Engine(store, queue, KernelCfg(
        retry_timeout=int(env.get("RETRY_TIMEOUT", 30_000))))
    worker = Worker(engine, clock, pid=env.get("K_REVISION", "local"),
                    ttl=int(env.get("LEASE", 60_000)))
    return Server(engine, worker, clock,
                  account=env.get("ROUTES_ACCOUNT"),
                  audience=env.get("AUDIENCE"))


def backends(env: Mapping[str, str]):
    """The store, the queue, and the clock they run on."""
    if env.get("SIMULATED"):
        return store_mem.Store(), queue_mem.Queue(), Clock()
    store = store_gcp.Store(env["BUCKET"])  # pragma: no cover - needs credentials
    queue = queue_gcp.Queue(  # pragma: no cover
        project=env["PROJECT"],
        location=env["LOCATION"],
        queue=env["QUEUE"],
        base_url=env["BASE_URL"],
        service_account=env.get("ROUTES_ACCOUNT"),
    )
    return store, queue, wall_clock  # pragma: no cover


def register_targets(env: Mapping[str, str]) -> None:
    """Say where each registered function runs: here, unless named otherwise."""
    base = env.get("BASE_URL", "").rstrip("/")
    if base:
        for name, _version in REGISTRY:
            TARGETS.setdefault(name, f"{base}/")
    for name, url in json.loads(env.get("ROUTES_WORKERS", "{}")).items():
        TARGETS[name] = url


def install_tracing(env: Mapping[str, str]) -> None:
    try:
        from . import otel_gcp

        otel_gcp.install(project=env.get("PROJECT"),
                         service=env.get("K_SERVICE", "durable-execution"),
                         instance=env.get("K_REVISION", "local"))
    except Exception as e:  # pragma: no cover - needs a broken environment
        logging.getLogger(__name__).warning("tracing is off: %s", e)


def wall_clock() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)
