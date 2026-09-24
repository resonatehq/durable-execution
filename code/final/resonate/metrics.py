"""What the store was asked to do, and how it answered.

The numbers that matter most about this engine are not counters at all --
how many runs are unsettled, how old the oldest one is, how long a run
takes from `created_at` to `settled_at` -- and none of those can be counted
in a process, because a stuck run is precisely one where no process is
running. They are a sweep of the bucket, and the document already holds
them.

What a process *can* say is what it just did. A conditional write refused
at 412 is the interesting one: it is not an error, it is the engine losing
a race and re-deciding, and the rate of it is the difference between a busy
origin and a broken one. `store_gcp.py` records 2.6 retries per successful
write to one object under eight concurrent writers; that number came from a
one-off script, and this is how it would come from production instead.

## Why this wraps the port rather than the implementations

There are two stores and there will be more. Instrumenting each one means
every new one starts uninstrumented and the two drift, so `counting` wraps
anything that implements `StoreP`: the simulator and the bucket are counted
by the same code, and `store_gcp.py` has no line of instrumentation in it.

`tracing.watch` does the same thing one step more generically, because a
trace only has to record that a call happened. A counter has to know what
an *outcome* means -- that a `get` returning `None` is a miss, that a
`Conflict` from a `put` is a refusal and not a failure -- and that meaning
belongs to the port's contract in `spec/store.py`. So this knows the
store's vocabulary and nothing about either implementation, and
`test_metrics.py` runs the wrapper through the store conformance suite to
show it is still a store.

## Deltas, because the process is going to die

A Cloud Run container may handle one request and exit. A counter that only
ever goes up is worthless if nobody reads it before the process goes away,
and a periodic exporter on a sixty-second timer never gets the chance --
which is the same problem scraping has, wearing a different hat.

So `drain()` reads and resets, and the shell emits after each request. Two
requests in flight at once may split a count between them; nothing is lost,
because the totals add. What is lost is the process crashing between the
last write and the emit, which costs one request's counts and is the reason
this is a metric and not a ledger.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .ports import Conflict, Unavailable

#: One label set, sorted, so two spellings of one thing are one key.
Key = tuple[str, tuple[tuple[str, str], ...]]


@dataclass
class Counters:
    """Event counts, since whoever last drained them.

    Locked because a container serves many requests at once and `d[k] += 1`
    is a read, an add and a write -- three chances to lose one under
    threads, which is exactly the kind of loss that makes a rate look
    plausible and be wrong.
    """

    counts: dict[Key, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, metric: str, n: int = 1, **labels: str) -> None:
        key = (metric, tuple(sorted(labels.items())))
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + n

    def snapshot(self) -> dict[Key, int]:
        """A copy, leaving the counts where they are. For a test, or for a
        route that reports without consuming."""
        with self.lock:
            return dict(self.counts)

    def drain(self) -> dict[Key, int]:
        """Everything since the last drain, and start again from nothing."""
        with self.lock:
            out, self.counts = self.counts, {}
            return out

    def total(self, metric: str, **labels: str) -> int:
        """One count, for a test to assert on."""
        return self.snapshot().get((metric, tuple(sorted(labels.items()))), 0)


#: The process's own counters. A module global because the shell has one
#: store and one place it emits from; a test passes its own instead.
COUNTERS = Counters()


# ---------------------------------------------------------------------------
# The instrument
# ---------------------------------------------------------------------------

#: What the store did, as the port's contract describes it rather than as
#: any implementation spells it.
#:
#:   get      hit | miss
#:   put      created | replaced | refused
#:   delete   ok
#:   list     ok
#:
#: and `unavailable` on any of them, which is the one outcome that says
#: nothing about whether the write landed.
STORE_OPS = ("get", "put", "delete", "list")


class counting:
    """Any `StoreP`, counted. Transparent in every other respect.

    It returns what the inner store returns and raises what it raises, in
    the same cases -- which is not a claim to take on faith:
    `test_metrics.py` runs this through `spec.store.conformance`, the same
    eleven claims every store is held to.
    """

    def __init__(self, inner: Any, counters: Counters | None = None) -> None:
        self._inner = inner
        self._counters = counters if counters is not None else COUNTERS

    def __repr__(self) -> str:
        return f"counting({self._inner!r})"

    def _count(self, op: str, outcome: str) -> None:
        self._counters.add("store", op=op, outcome=outcome)

    def get(self, key: str):
        try:
            found = self._inner.get(key)
        except Unavailable:
            self._count("get", "unavailable")
            raise
        self._count("get", "miss" if found is None else "hit")
        return found

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        # What was asked for, decided before the call, so a refusal is
        # attributed to the write it refused rather than to a guess made
        # afterwards from an exception.
        asked = "created" if if_absent else ("replaced" if if_match else "written")
        try:
            version = self._inner.put(key, body, if_match=if_match, if_absent=if_absent)
        except Conflict:
            # Not an error. The engine lost a race and will re-decide, and
            # the rate of this is the load on a single origin.
            self._count("put", "refused")
            raise
        except Unavailable:
            self._count("put", "unavailable")
            raise
        self._count("put", asked)
        return version

    def delete(self, key: str) -> None:
        try:
            self._inner.delete(key)
        except Unavailable:
            self._count("delete", "unavailable")
            raise
        self._count("delete", "ok")

    def list(self, prefix: str, limit: int) -> list[str]:
        try:
            keys = self._inner.list(prefix, limit)
        except Unavailable:
            self._count("list", "unavailable")
            raise
        self._count("list", "ok")
        return keys


# ---------------------------------------------------------------------------
# Getting them out of the process
# ---------------------------------------------------------------------------

#: Where a drained batch goes. `None` is off, and off is the default.
_SINK: Callable[[dict[Key, int]], None] | None = None


def to(sink: Callable[[dict[Key, int]], None] | None) -> None:
    global _SINK
    _SINK = sink


def sink() -> Callable[[dict[Key, int]], None] | None:
    return _SINK


def on() -> bool:
    return _SINK is not None


def emit(counters: Counters | None = None) -> None:
    """Drain and hand over. Called by the shell after each request.

    Nothing raises out of here. A count is what you look at when something
    else went wrong, and observability that can fail a request is
    observability that can take the service down.
    """
    out = _SINK
    if out is None:
        return
    batch = (counters if counters is not None else COUNTERS).drain()
    if not batch:
        return
    try:
        out(batch)
    except Exception:  # pragma: no cover - a broken sink, not a broken engine
        pass


def as_lines(batch: dict[Key, int]) -> Iterator[dict[str, Any]]:
    """One structured record per counted event, ready for a log.

    Logging rather than pushing, by default, for the reason the rest of
    this module is built around: Cloud Run captures stdout synchronously,
    so there is no exporter to flush and nothing to lose when the container
    exits a millisecond later. A log-based metric turns these into a
    counter in Cloud Monitoring without a line of push code, and a real
    OTLP exporter is another `to()` away when one is wanted.
    """
    for (metric, labels), n in sorted(batch.items()):
        yield {"metric": f"de.{metric}", "count": n, **dict(labels)}


def log_sink(batch: dict[Key, int]) -> None:
    """The default sink: one JSON object per line on stdout."""
    for line in as_lines(batch):
        print(json.dumps(line), flush=True)
