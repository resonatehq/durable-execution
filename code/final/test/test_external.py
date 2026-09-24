"""Waiting for somebody outside, which is what a signal is for.

Other durable systems give you a signal handler, a wait condition, and a
mutable field between them: the handler writes, the condition reads, and
the workflow's memory holds the value in between. That memory is the part
this system does not have. A run here has no state of its own -- it
replays from the top and reads its calls back by position -- so the thing
being waited on has to *be* a promise, in the bucket, with an id anyone can
find.

That turns out to be a smaller mechanism rather than a bigger one. There is
no handler to register, nothing to hold, and no condition to re-evaluate:
the run suspends on a pending promise exactly as it suspends on an `rpc`,
and whoever has the answer settles it over the same protocol route a client
already uses. The tests below are that claim, one piece at a time.
"""

from __future__ import annotations

import json

import pytest

from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg, TAG_EXTERNAL
from resonate.types import PromiseSettle
from resonate.queue_mem import Queue
from resonate.testing.sim import Clock, Runtime
from resonate.worker import Worker
from resonate.sdk import Failed, dumps, external, resonate
from resonate.types import SWEEP
from resonate.store_mem import Store

CFG = KernelCfg(retry_timeout=30_000)
WORKER = "worker://w"
ORIGIN = "approval.1"

#: Deliberately shorter than a day and longer than the retry timeout, so a
#: test can expire it without the task's own deadline firing first.
PATIENCE = 120_000

RAN: list[str] = []


@resonate
async def needs_a_human(amount: int):
    RAN.append("asked")
    answer = await external(ask={"approve": amount}, timeout=PATIENCE)
    RAN.append(f"heard:{answer}")
    return {"paid": amount} if answer == "yes" else {"refused": amount}


def world():
    RAN.clear()
    store, queue, clock = Store(), Queue(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(WORKER, Worker(engine, clock, "w-1"), needs_a_human)
    return rt, store, engine, clock


def document(store, origin=ORIGIN):
    found = store.get(doc_key(origin))
    return decode(found[0].encode(), origin) if found else None


def waiting_on(store):
    """The pending question, found the way a client would find it: by
    reading the document, not by being told."""
    doc = document(store)
    return [o for o in doc.objects
            if o.promise.tags.get(TAG_EXTERNAL) == "true"
            and o.promise.state == "pending"]


def test_the_run_suspends_on_a_promise_anyone_can_find():
    rt, store, _, _ = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()

    assert RAN == ["asked"], RAN
    pending = waiting_on(store)
    assert len(pending) == 1, [o.id for o in document(store).objects]
    assert pending[0].id == f"{ORIGIN}:1", "the id is a position, like every other call"
    assert json.loads(pending[0].promise.param.data) == {"ask": {"approve": 500}}


def test_settling_it_wakes_the_run():
    """The whole mechanism. No handler was registered and nothing held the
    question in memory -- the answer goes to the promise, and the promise
    is what the run was waiting on."""
    rt, store, engine, clock = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()
    asked = waiting_on(store)[0].id

    engine.process(PromiseSettle(asked, "resolved", dumps("yes")), clock())
    rt.drain()

    root = document(store).get(ORIGIN).promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == {"paid": 500}
    # `asked` twice, `heard` once. Replay re-runs the body -- that is what
    # replay is -- and the durable part is the question: it is one promise,
    # at one position, and waking reads the answer back rather than asking
    # again. A second external promise here would mean a second question to
    # a person who already answered.
    assert RAN == ["asked", "asked", "heard:yes"], RAN
    assert len([o for o in document(store).objects
                if o.promise.tags.get(TAG_EXTERNAL) == "true"]) == 1


def test_the_answer_is_whatever_was_sent():
    rt, store, engine, clock = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()
    engine.process(PromiseSettle(waiting_on(store)[0].id, "resolved", dumps("no")), clock())
    rt.drain()
    assert json.loads(document(store).get(ORIGIN).promise.value.data) == {"refused": 500}


def test_nothing_is_running_while_it_waits():
    """Not a parked coroutine, not a held task, not a container kept alive.
    Draining again at the same instant does nothing, because there is
    nothing to do -- the only thing left is a row in a bucket."""
    rt, store, _, _ = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()
    assert rt.drain() == 0
    assert RAN == ["asked"], RAN


def test_a_question_nobody_answers_expires():
    """An external promise is not a timer, so its deadline rejects rather
    than resolves, and the `await` raises. A confirmation nobody gave is
    not a confirmation, which is the behaviour you want and the reason the
    timeout is the caller's to choose."""
    rt, store, _, clock = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()

    clock.advance(PATIENCE)
    rt.drain()

    root = document(store).get(ORIGIN).promise
    assert root.state == "rejected", root.state
    assert "Failed" in root.value.data, root.value.data
    assert "heard" not in " ".join(RAN), \
        "it carried on as though it had been answered"


def test_the_deadline_is_armed_so_it_cannot_hang_for_ever():
    """The bug this would otherwise have. A suspended task is disarmed, so
    if the question's own deadline were not armed the run would sleep past
    it and be collected days later by the root's timeout instead."""
    rt, store, _, clock = world()
    rt.start(ORIGIN, needs_a_human, 500)
    rt.drain()

    armed = sorted(e.not_before for e in rt.queue.entries.values()
                   if e.url.startswith(SWEEP))
    assert armed, "no sweep armed at all"
    assert min(armed) == clock() + PATIENCE, (
        f"earliest sweep is {min(armed)}, not the question's deadline "
        f"{clock() + PATIENCE}: nobody will ever notice it went unanswered")


def test_a_negative_deadline_is_refused():
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(external(ask="x", timeout=-1))
