"""A sleep waits, and then wakes.

The second half is the one that needs a test. Waiting is easy to get right
by accident -- a run that never resumes has also, technically, not returned
early. Waking is what `sleep` promises, and it depends on something no other
test looks at: whether the shell armed a deadline for the timer at all.

The chain is long enough to be worth naming. `sleep` creates a promise
tagged `resonate:timer`, which resolves rather than rejects when its
deadline passes. The worker awaiting it suspends, and `task_suspend`
disarms the task, so the task's own deadlines are gone. If the timer's
deadline is not armed either, `min_deadline` skips it and the shell arms
the next earliest thing instead -- in practice the root promise's own
timeout, days away. The run does not hang and does not error. It sleeps
past its deadline, and the eventual sweep finds the root expired and
rejects it. A sleep of five seconds becomes a rejection in eleven days,
which is worse than a crash because nothing reports it.

That is why `timeout_armed` follows `is_external` rather than asking for a
target: a timer is external, so it is armed. The suite passed both spellings
before this file existed, and passed a third that special-cased the timer
tag, which is the gap this closes.
"""

from __future__ import annotations

import json

from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg, TAG_TIMER
from resonate.testing.queue_mem import Queue
from resonate.testing.sim import Clock, Runtime
from resonate.worker import Worker
from resonate.sdk import resonate, sleep
from resonate.testing.store_mem import Store

CFG = KernelCfg(retry_timeout=30_000)
WORKER = "worker://napper"
ORIGIN = "nap.1"

#: Shorter than `retry_timeout`, on purpose. A sleep that outlives the retry
#: deadline would be woken by the task's own timer and prove nothing.
SLEEP_MS = 5_000

#: What the function actually did, in order. A sleep is allowed to replay the
#: work before it -- that is what memoisation is for -- but the work must not
#: happen twice.
RAN: list[str] = []


@resonate
async def watched_nap(label: str):
    RAN.append(f"before:{label}")
    await sleep(SLEEP_MS)
    RAN.append(f"after:{label}")
    return f"{label} woke"


def world():
    RAN.clear()
    store, queue, clock = Store(), Queue(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(WORKER, Worker(engine, clock, "napper-1"), watched_nap)
    return rt, store, engine, clock


def document(store: Store, origin: str = ORIGIN):
    found = store.get(doc_key(origin))
    return decode(found[0].encode()) if found else None


def root(store: Store):
    doc = document(store)
    return doc.get(ORIGIN).promise if doc else None


def sweeps(rt) -> list[int]:
    """When the queue has been told to come back, for this origin."""
    return sorted(e.not_before for n, e in rt.queue.entries.items()
                  if e.body["kind"] == "timeout")


# --- it waits --------------------------------------------------------------


def test_a_sleep_blocks_until_its_deadline():
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()

    assert RAN == ["before:a"], RAN
    assert root(store).state == "pending"

    # Draining again at the same instant changes nothing: the queue holds
    # work, but none of it is eligible yet. This is the difference between a
    # queue and a list.
    assert rt.drain() == 0
    assert RAN == ["before:a"], RAN


def test_the_sleep_is_a_timer_promise_that_resolves():
    """Not a rejection dressed up. The tag is what makes the deadline a
    result rather than a failure, and `promise_create` refuses a timer that
    also names a target."""
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()

    doc = document(store)
    timers = [o for o in doc.objects if o.promise.tags.get(TAG_TIMER) == "true"]
    assert len(timers) == 1, [o.id for o in doc.objects]
    timer = timers[0]
    assert timer.promise.target() is None
    assert json.loads(timer.promise.param.data) == {"sleep": SLEEP_MS}


# --- and it wakes ----------------------------------------------------------


def test_the_shell_arms_a_deadline_for_the_sleep():
    """The step everything else depends on, asserted on its own.

    A suspended task is disarmed, so the timer's own deadline is the only
    thing that will wake this run on time. Something else usually remains
    armed -- the root promise's timeout, days out -- so the bug does not
    show up as an empty queue. It shows up as the earliest deadline being
    the wrong one, which is why this asserts the instant and not merely
    that a sweep exists.
    """
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()

    armed = sweeps(rt)
    assert armed, "no sweep armed at all"
    assert min(armed) == clock() + SLEEP_MS, (
        f"earliest sweep is {min(armed)}, not the sleep's deadline "
        f"{clock() + SLEEP_MS}: the run will not wake on time")


def test_a_sleep_wakes_and_the_run_finishes():
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()
    assert root(store).state == "pending"

    clock.advance(SLEEP_MS)
    rt.drain()

    promise = root(store)
    assert promise.state == "resolved", promise.state
    assert json.loads(promise.value.data) == "a woke"
    assert RAN.count("after:a") == 1, RAN


def test_the_work_before_the_sleep_is_not_done_twice():
    """The run replays from the top when it wakes, so `before` runs again --
    that is what replay is. What must not happen is the *durable* work being
    repeated, and here the sleep itself is that work: it is one promise, at
    one position, and waking reads it back rather than sleeping again."""
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()
    clock.advance(SLEEP_MS)
    rt.drain()

    doc = document(store)
    timers = [o for o in doc.objects if o.promise.tags.get(TAG_TIMER) == "true"]
    assert len(timers) == 1, "a second sleep would mean the run slept twice"
    assert timers[0].promise.state == "resolved"
    assert RAN.count("after:a") == 1, RAN


def test_waking_early_does_not_happen():
    """One millisecond short is still asleep. A deadline that fired on
    approximately the right tick would pass every other test in this file."""
    rt, store, _, clock = world()
    rt.start(ORIGIN, watched_nap, "a")
    rt.drain()

    clock.advance(SLEEP_MS - 1)
    rt.drain()
    assert root(store).state == "pending", "woke a millisecond early"
    assert RAN == ["before:a"], RAN

    clock.advance(1)
    rt.drain()
    assert root(store).state == "resolved"
