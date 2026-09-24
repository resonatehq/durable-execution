"""The simulated queue, and the research agent run over an unkind one.

A store that refuses a write is one failure. A queue has four, and they are
the ones the protocol's fencing and idempotence were built for: the same
message twice, messages out of order, messages late, and messages that
never arrive at all. None of them were exercised before this file.
"""

from __future__ import annotations

import json

import pytest

from resonate import queue_mem
from resonate.spec import queue as queue_spec
from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg
from resonate.queue_mem import Delivery, Queue
from resonate.types import HERE
from resonate.testing.sim import Clock, Runtime
from resonate.worker import Worker
from resonate.store_mem import Store
from test_e2e import (
    AGENT, CALLS, EXPECTED, ORIGIN, QUESTION, SEARCH, counted_agent,
    counted_research, counted_search,
)

CFG = KernelCfg(retry_timeout=30_000)


# --- the queue on its own --------------------------------------------------


def test_the_simulated_queue_satisfies_the_contract():
    """The same claims `queue_gcp` is held to. Everything below is about
    what a simulator can do that a real queue cannot be asked to."""
    assert queue_spec.conformance(queue_mem) == []


def test_nothing_is_eligible_before_its_time():
    q = Queue()
    q.create(HERE, {}, not_before=500)
    assert q.take(499) is None
    assert q.take(500) is not None


def test_an_acknowledged_task_is_gone():
    q = Queue()
    q.create("w", "hello")
    d = q.take(0)
    q.ack(d, 0)
    assert q.take(0) is None


def test_a_lost_acknowledgement_is_a_second_delivery():
    """At-least-once, stated as what actually happens: the handler ran, and
    the queue never found out."""
    q = Queue(duplicate=1.0, backoff=10)
    q.create("w", "hello")
    first = q.take(0)
    q.ack(first, 0)
    assert q.take(0) is None, "a lost answer still waits its backoff"
    second = q.take(10)
    assert second is not None and second.name == first.name
    assert second.attempt == 2


def test_a_task_that_is_never_answered_is_eventually_dropped():
    q = Queue(give_up_after=3, backoff=10)
    q.create("w", "hello")
    now = 0
    for _ in range(3):
        d = q.take(now)
        assert d is not None
        q.nack(d, now)
        now += 10
    assert q.take(now) is None and q.dropped


def test_deleting_cancels_a_deadline():
    q = Queue()
    name = q.create(HERE, {}, not_before=100)
    q.delete(name)
    assert q.take(1_000) is None


def test_deleting_what_is_gone_succeeds():
    Queue().delete("task-99")


def test_the_order_is_nobody_s_promise():
    q = Queue(seed=7, shuffle=True)
    for i in range(6):
        q.create(f"w{i}", i)
    out = [q.take(0) for _ in range(6)]
    for d in out:
        q.ack(d, 0)
    assert [d.body for d in out] != list(range(6)), "shuffling did not shuffle"


# --- the counted_agent, over an unkind queue ---------------------------------------


def cloud(**knobs):
    CALLS.clear()
    store = Store()
    queue = Queue(**knobs)
    clock = Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(AGENT, Worker(engine, clock, "agent-1"), counted_research, counted_agent)
    rt.serve(SEARCH, Worker(engine, clock, "search-1"), counted_search)
    return rt, store, queue, clock


def settle(rt, clock, rounds: int = 12) -> None:
    for _ in range(rounds):
        rt.drain()
        clock.advance(40_000)
    rt.drain()


def answer(store):
    found = store.get(doc_key(ORIGIN))
    assert found, "nothing was ever written"
    doc = decode(found[0].encode(), ORIGIN)
    root = doc.get(ORIGIN).promise
    assert root.state == "resolved", f"the run did not finish: {root.state}"
    return json.loads(root.value.data)


DONE = {"agent": 2, "search:durable execution": 1,
        "search:workflow recovery": 1, "search:sagas": 1}


def test_the_agent_runs_over_a_well_behaved_queue():
    rt, store, queue, clock = cloud()
    rt.start(ORIGIN, counted_research, QUESTION)
    settle(rt, clock)
    assert answer(store) == EXPECTED and dict(CALLS) == DONE


def test_every_message_twice_costs_nothing():
    """A duplicate `execute` finds the task already claimed, at a version it
    does not hold, and is refused. That refusal is the fence doing its job,
    and it is why at-least-once delivery is safe to build on."""
    rt, store, queue, clock = cloud(duplicate=1.0)
    rt.start(ORIGIN, counted_research, QUESTION)
    settle(rt, clock)
    assert answer(store) == EXPECTED
    assert dict(CALLS) == DONE, "something was paid for twice"
    assert queue.delivered > 12, "the duplicates did not happen"


@pytest.mark.parametrize("seed", range(8))
def test_out_of_order_and_late_and_sometimes_lost(seed):
    """All four at once, eight different ways. The run finishes, the answer
    is the same, and nothing is done twice."""
    rt, store, queue, clock = cloud(
        seed=seed, duplicate=0.4, shuffle=True, lateness=500, lose=0.3, backoff=100)
    rt.start(ORIGIN, counted_research, QUESTION)
    settle(rt, clock, rounds=30)
    assert answer(store) == EXPECTED
    assert dict(CALLS) == DONE


# --- the hole, demonstrated rather than hidden -----------------------------


class DropsEverySweep(Queue):
    """A queue that gives up on every deadline and only on those."""

    def take(self, now):
        d = super().take(now)
        if d is not None and d.body["kind"] == "timeout":
            self.entries.pop(d.name, None)
            self.dropped.append(d.name)
            return self.take(now)
        return d


def test_a_dropped_deadline_is_the_one_thing_nothing_repairs():
    """A dropped `execute` is recoverable: the task's retry deadline was
    committed before the message left. A dropped *sweep* is not, because the
    deadline it carried is the only thing that was going to fire.

    This is not a defect in the engine. It is a condition a deployment has
    to meet, and the remedy is below: a sweep that does not depend on any
    single queued task.
    """
    rt, store, queue, clock = cloud()
    rt.queue = queue = DropsEverySweep(seed=1)
    rt.engine.queue = queue
    rt.start(ORIGIN, counted_research, QUESTION)
    settle(rt, clock)
    found = store.get(doc_key(ORIGIN))
    root = decode(found[0].encode(), ORIGIN).get(ORIGIN).promise
    assert root.state == "resolved", (
        "with nothing suspended on a deadline this still finishes; if it ever "
        "does not, the fan-out has started depending on a sweep")


def test_a_periodic_sweep_recovers_what_the_queue_lost():
    """The remedy. Something that walks the bucket on its own schedule, so a
    deadline that was only ever in a queued task is not the only way a task
    is offered again."""
    rt, store, queue, clock = cloud()
    rt.queue = queue = DropsEverySweep(seed=1)
    rt.engine.queue = queue
    rt.start(ORIGIN, counted_research, QUESTION)
    for _ in range(12):
        rt.drain()
        clock.advance(40_000)
        # What a Cloud Scheduler job posting a timeout would do.
        rt.handle(Delivery("periodic", HERE, {"kind": "timeout", "origin": ORIGIN}, 1))
    rt.drain()
    assert answer(store) == EXPECTED and dict(CALLS) == DONE


# --- what is scheduled, and when ------------------------------------------


def test_the_deadline_is_scheduled_before_the_document_commits():
    """The order the whole crash story rests on, watched through the queue
    and the bucket at once rather than through a log the engine kept.

    A committed document whose deadline was never scheduled is the one state
    nothing repairs: the promise never times out, the task is never offered
    again, and every answer about it stays correct forever. So the task goes
    in first, and a queue that will not take it fails the request instead of
    committing anyway.

    A dispatch goes the other way, after the commit, so a message is always
    a consequence of committed state rather than of an intention.
    """
    log: list[str] = []

    class WatchedQueue(Queue):
        def create(self, url, body, *, not_before=0):
            name = super().create(url, body, not_before=not_before)
            log.append(f"schedule {url} at {not_before}")
            return name

        def delete(self, name):
            log.append("cancel")
            super().delete(name)

    class WatchedBlob(Store):
        def put(self, key, body, **kw):
            version = super().put(key, body, **kw)
            log.append("commit")
            return version

    queue, store, clock = WatchedQueue(), WatchedBlob(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(AGENT, Worker(engine, clock, "w"), counted_research, counted_agent)
    rt.serve(SEARCH, Worker(engine, clock, "s"), counted_search)

    CALLS.clear()
    rt.start(ORIGIN, counted_research, QUESTION)
    assert log == [
        f"schedule {HERE} at 30000",            # the deadline, first
        "commit",                               # then the state
        f"schedule {AGENT} at 0",               # then the message
    ]


def test_only_deadlines_carry_a_schedule():
    """There is one scheduled shape in the system, and it is the deadline.
    A dispatch is created with no schedule, because a dispatch is not
    deferred: anything that must wait — a durable sleep, a delay tag, a
    retry backoff — waits by having a deadline, and the deadline is what
    gets scheduled."""
    rt, store, queue, clock = cloud()
    rt.start(ORIGIN, counted_research, QUESTION)
    settle(rt, clock)
    scheduled = [(e.body["kind"], e.not_before) for e in queue.entries.values()]
    assert all(not_before == 0 or kind == "timeout" for kind, not_before in scheduled), scheduled
