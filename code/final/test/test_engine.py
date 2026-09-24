"""The engine: the codec, the write law, the effect order, and every window
the process can stop in.

The kernel's suites prove what a transition *is*. These prove that what the
engine does to a bucket is that transition, once, and that stopping anywhere
in the middle leaves something that repairs itself.
"""

from __future__ import annotations

import pytest

from resonate.codec import decode, doc_key, encode
from resonate.engine import Engine, origin_of_msg
from resonate.types import Timeout
from resonate.kernel import (
    Document, KernelCfg, PENDING, REJECTED_TIMEDOUT, RESOLVED, T_ACQUIRED,
    T_FULFILLED, T_PENDING, check_invariants,
)
from resonate.types import (
    Execute, PromiseCreate, PromiseGet, PromiseRegisterListener,
    PromiseSettle, TaskAcquire, TaskFulfill, TaskSuspend, Value,
)
from resonate.errors import Conflict
from resonate.testing.faults import Crash, Fault
from resonate.testing.queue_mem import Queue
from resonate.testing.store_mem import Store
from resonate.types import decode_message

W = "http://w"
CFG = KernelCfg(retry_timeout=30_000)


def build(fault=None):
    store, queue = Store(fault), Queue(fault=fault)
    return Engine(store, queue, CFG), store, queue


def armed(q):
    """The deadlines the queue is holding, by name.

    A deadline and a dispatch are the same kind of task, so what tells them
    apart here is what tells them apart in production: the message it
    carries. A timeout comes back to this service.
    """
    return {n: (e.body["origin"], e.not_before)
            for n, e in q.entries.items() if e.body["kind"] == "timeout"}


def sent(q):
    """The dispatches, decoded, and taken off the queue."""
    out = [(e.url, decode_message(e.body)) for e in q.entries.values()
           if e.body["kind"] != "timeout"]
    for n in [n for n, e in q.entries.items() if e.body["kind"] != "timeout"]:
        q.entries.pop(n)
    return out


def create(id, to=100_000, tags=None):
    return PromiseCreate(id, to, Value(), tags if tags is not None else {"resonate:target": W})


def read(store, origin="o"):
    found = store.get(doc_key(origin))
    return Document() if found is None else decode(found[0].encode())


def substance(doc):
    """What two runs must agree on. Generations and timer names are the
    shell's own bookkeeping and differ between a clean run and a recovered
    one; the promises and tasks may not."""
    return [(o.id, o.promise, o.task) for o in doc.objects]


# --- the codec -------------------------------------------------------------


def test_a_document_round_trips():
    e, store, q = build()
    e.process(create("o:a"), 0)
    e.process(TaskAcquire("o:a", 0, "p1", 5_000), 10)
    e.process(PromiseRegisterListener("o:a", "http://l"), 20)
    doc = decode(store.get(doc_key("o"))[0].encode())
    assert decode(encode(doc)) == doc


def test_every_origin_gets_its_own_key():
    assert doc_key("order-7") == "wf/order-7"
    assert doc_key("テスト") == "wf/%E3%83%86%E3%82%B9%E3%83%88"
    assert doc_key("a/b:c") == "wf/a%2Fb%3Ac"


def test_the_origin_is_read_off_whichever_id_the_message_carries():
    assert origin_of_msg(create("o:a")) == "o"
    assert origin_of_msg(Timeout("o")) == "o"
    assert origin_of_msg(TaskAcquire("o:a:1", 0, "p", 1)) == "o"


# --- the write law ---------------------------------------------------------


def test_a_read_that_changes_nothing_writes_nothing():
    e, store, q = build()
    e.process(create("o:a"), 0)
    before = store.objects[doc_key("o")]
    assert e.process(PromiseGet("o:a"), 1).status == 200
    assert store.objects[doc_key("o")] == before, "same bytes, same generation"


def test_a_read_past_a_deadline_does_write_because_it_settles():
    e, store, q = build()
    e.process(create("o:a", to=1_000), 0)
    version = store.objects[doc_key("o")][1]
    assert e.process(PromiseGet("o:a"), 5_000).data["promise"]["state"] == "rejected_timedout"
    assert store.objects[doc_key("o")][1] != version, "the settlement was not written"


def test_the_clock_alone_is_not_worth_a_write():
    e, store, q = build()
    e.process(create("o:a"), 0)
    before = store.objects[doc_key("o")]
    e.process(PromiseGet("o:a"), 999)
    assert store.objects[doc_key("o")] == before


def test_a_regressed_clock_cannot_un_expire_anything():
    e, store, q = build()
    e.process(create("o:a", to=1_000), 0)
    e.process(PromiseGet("o:a"), 5_000)  # settles it, and folds the clock to 5_000
    assert read(store).clock == 5_000
    e.process(create("o:b", to=2_000), 0)  # a caller whose clock ran backwards
    assert read(store).get("o:b").promise.state == REJECTED_TIMEDOUT


# --- the effect order ------------------------------------------------------


def test_the_effects_go_in_the_order_every_crash_window_survives():
    fault = Fault()
    e, store, q = build(fault)
    e.process(create("o:a"), 0)
    # An arm and a send are the same call to the same port, told apart the
    # way the deployment tells them apart: by the message's `kind`.
    assert fault.log == ["create timeout /", f"commit {doc_key('o')}", "create execute http://w"]

    fault.log = []
    e.process(TaskAcquire("o:a", 0, "p1", 5_000), 100)
    # The lease is armed before the commit, and the retry it replaces is only
    # removed once the commit that owned it is gone.
    assert fault.log == ["create timeout /", f"commit {doc_key('o')}", "delete task-1"]
    assert list(armed(q).values()) == [("o", 5_100)], "one deadline per origin"


def test_a_disarm_names_the_deadline_its_own_predecessor_armed():
    e, store, q = build()
    e.process(create("o:a"), 0)
    first = read(store).timer_name
    e.process(TaskAcquire("o:a", 0, "p1", 5_000), 100)
    assert first not in armed(q) and read(store).timer_name in armed(q)


# --- losing a race ---------------------------------------------------------


def test_a_second_writer_on_a_stale_generation_is_refused():
    store, queue = Store(), Queue()
    a = Engine(store, queue, CFG)
    b = Engine(store, queue, CFG)
    a.process(create("o:a"), 0)
    # Both read the same version; the first to write wins.
    body, version = store.get(doc_key("o"))
    a.process(PromiseSettle("o:a", RESOLVED), 10)
    with pytest.raises(Conflict):
        b.store.put(doc_key("o"), body, if_match=version)


def test_a_conflict_reaches_the_caller_rather_than_being_retried_here():
    """The engine never loops. A loop would pick a retry policy — how many
    times, how long, whether a re-decided request is the same request —
    before anything has said what it should be."""
    class Racing(Store):
        def put(self, key, body, **conditions):
            raise Conflict("someone else got there first")

    e = Engine(Racing(), Queue(), CFG)
    with pytest.raises(Conflict):
        e.process(create("o:a"), 0)


# --- crash windows ---------------------------------------------------------

#: A run that touches every effect the engine can perform: an arm and a send
#: on the create, an arm and a disarm on the acquire, a disarm on the fulfil.
SCRIPT = [
    (create("o:a"), 0),
    (PromiseRegisterListener("o:a", "http://l"), 10),
    (TaskAcquire("o:a", 0, "p1", 5_000), 20),
    (TaskFulfill("o:a", 1, PromiseSettle("o:a", RESOLVED, Value(data="v"))), 30),
]


def run_script(e, script):
    for msg, now in script:
        e.process(msg, now)


def settle_down(e, q, now, limit=20):
    """Deliver every deadline that is due, as the queue eventually would.
    Each re-arm is strictly in the future, so this terminates."""
    for _ in range(limit):
        due = [(n, o) for n, (o, at) in armed(q).items() if at <= now]
        if not due:
            return
        for name, origin in due:
            q.entries.pop(name, None)
            e.process(Timeout(origin), now)
    raise AssertionError("the sweep did not settle down")


def test_the_clean_run_is_what_a_crashed_one_must_reach():
    e, store, q = build()
    run_script(e, SCRIPT)
    doc = read(store)
    assert doc.get("o:a").promise.state == RESOLVED
    assert doc.get("o:a").task.state == T_FULFILLED
    assert doc.timer_at is None and not armed(q), "nothing left armed"
    assert any(a == "http://l" for a, _ in sent(q)), "the listener heard"


@pytest.mark.parametrize("k", range(12))
def test_stopping_at_any_effect_leaves_something_that_repairs_itself(k):
    """Cut the power at the k-th write the engine attempts, anywhere across
    the store and the queue. Then do what the world does: the
    caller retries its request, and the deadlines fire. The run must reach the
    same promises and tasks as one that was never interrupted."""
    clean_engine, clean_store, _ = build()
    run_script(clean_engine, SCRIPT)
    want = substance(read(clean_store))

    fault = Fault()
    e, store, q = build(fault)
    fault.crash_after(k)
    crashed_at = None
    for i, (msg, now) in enumerate(SCRIPT):
        try:
            e.process(msg, now)
        except Crash:
            crashed_at = i
            break
    if crashed_at is None:
        pytest.skip(f"the script performs fewer than {k + 1} writes")

    # The document is readable and consistent whatever happened.
    assert check_invariants(read(store)) is None

    fault.heal()
    for msg, now in SCRIPT[crashed_at:]:
        e.process(msg, now)
    settle_down(e, q, 10_000_000)
    assert substance(read(store)) == want


def test_a_write_that_landed_but_reported_failure_is_recovered_by_a_retry():
    """The window nothing can close over a network: the commit landed and the
    answer was lost. The caller cannot tell, so it retries, and every
    operation is idempotent — the retry reads the promise its own lost write
    created."""
    fault = Fault()
    e, store, q = build(fault)
    store.land_then_fail = True
    fault.crash_after(1)  # let the arm through, land the commit, then fail
    with pytest.raises(Crash):
        e.process(create("o:a"), 0)
    assert read(store).get("o:a") is not None, "the write did land"
    fault.heal()
    reply = e.process(create("o:a"), 0)
    assert reply.status == 200 and reply.data["promise"]["createdAt"] == 0


def test_an_orphan_deadline_fires_into_a_sweep_that_writes_nothing():
    """What is left when the power goes out between arming and committing."""
    fault = Fault()
    e, store, q = build(fault)
    fault.crash_after(1)  # the arm lands, the commit does not
    with pytest.raises(Crash):
        e.process(create("o:a"), 0)
    assert armed(q) and store.objects == {}, "a deadline nothing points at"
    fault.heal()
    e.process(Timeout("o"), 10_000_000)
    assert store.objects == {}, "the sweep found nothing due and wrote nothing"


# --- end to end ------------------------------------------------------------


def test_a_remote_call_through_the_engine():
    """Post 002, over a bucket: the caller suspends, another worker settles
    the callee, and the settle is what dispatches the caller again."""
    e, store, q = build()
    e.process(create("run"), 0)
    assert sent(q) == [(W, Execute("run", 0))], "the run is offered"

    e.process(TaskAcquire("run", 0, "w1", 5_000), 1)
    e.process(create("run:1"), 2)                                  # the call creates the rpc
    assert sent(q) == [(W, Execute("run:1", 0))]
    e.process(TaskSuspend("run", 1, ("run:1",)), 3)                 # Blocked: park

    e.process(TaskAcquire("run:1", 0, "w2", 5_000), 4)             # another worker takes it
    e.process(TaskFulfill("run:1", 1, PromiseSettle("run:1", RESOLVED, Value(data="42"))), 5)
    assert sent(q) == [(W, Execute("run", 1))], "the settle woke the caller"

    e.process(TaskAcquire("run", 1, "w3", 5_000), 6)               # any worker resumes it
    replay = e.process(create("run:1"), 7)                         # replay: create is a read
    assert replay.data["promise"]["value"] == {"data": "42"}
    e.process(TaskFulfill("run", 2, PromiseSettle("run", RESOLVED, Value(data="done"))), 8)

    doc = read(store, "run")
    assert doc.get("run").promise.state == RESOLVED
    assert doc.timer_at is None and not armed(q)
