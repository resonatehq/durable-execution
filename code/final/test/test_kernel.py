"""Ported from crates/resonate-server-blob/src/kernel/handle.rs, `mod tests`.

Every step also runs the whole conformance catalogue (`properties.py`). The
catalogue is stated per abstract step, and `handle_external` fuses two of
them, the sweep and the request, so each is checked on its own half: the
sweep from the document that came in to the swept document, as an internal
step; the request from the swept document to the document that goes out, as
an external step. The fused result is then held equal to the composition. A
step whose pre-state was built by hand rather than reached through the kernel
passes `legal_pre=False` and is checked against the kernel's own invariants
only.
"""

from properties import State, internal_failures, state_failures, trans_failures  # noqa: E402
from kernel import (
    PENDING, REJECTED_TIMEDOUT, RESOLVED, T_FULFILLED, T_PENDING, TAG_TIMER,
    T_ACQUIRED, DelTimeout, Document, Execute, KernelCfg, PromiseCreate, Reply,
    Send, SetDocument, SetTimeout, Task, Value, check_invariants, dewey,
    handle_external, handle_internal,
)

W = "http://worker:9999"
CFG = KernelCfg(retry_timeout=30_000)


def step(doc, req, now, legal_pre=True):
    """Apply a request: the new document, the sends, and the reply."""
    fx, reply = handle_external(doc, req, now, CFG)
    docs = [e.doc for e in fx if isinstance(e, SetDocument)]
    assert len(docs) == 1
    assert check_invariants(docs[0]) is None, check_invariants(docs[0])
    sends = [e for e in fx if isinstance(e, Send)]
    if legal_pre:
        halves(doc, req, now, docs[0])
    return docs[0], sends, reply, fx


def halves(doc, req, now, fused):
    """Check the catalogue on the two abstract steps the fused transition is
    made of, and that the fused document is their composition."""
    before = State(doc)
    swept = handle_internal(doc, now, CFG)
    mid = before.after(next(e.doc for e in swept if isinstance(e, SetDocument)), [e for e in swept if isinstance(e, Send)])
    assert state_failures(now, before) == [], state_failures(now, before)
    assert state_failures(now, mid) == [], state_failures(now, mid)
    assert trans_failures(now, before, mid) == [], trans_failures(now, before, mid)
    assert internal_failures(now, before, mid) == [], internal_failures(now, before, mid)
    fx2, _ = handle_external(mid.doc, req, now, CFG)  # its own sweep is a no-op now
    after = mid.after(next(e.doc for e in fx2 if isinstance(e, SetDocument)), [e for e in fx2 if isinstance(e, Send)])
    assert state_failures(now, after) == [], state_failures(now, after)
    assert trans_failures(now, mid, after) == [], trans_failures(now, mid, after)
    assert after.doc == fused, "fused != composed"


def create(id, timeout_at, tags=None):
    return PromiseCreate(id=id, timeout_at=timeout_at, param=Value(), tags=tags or {})


def with_targeted(id, timeout_at):
    doc, _, reply, _ = step(Document(), create(id, timeout_at, {"resonate:target": W}), 0)
    assert reply.status == 200
    return doc


# --- create ----------------------------------------------------------------


def test_creating_an_untargeted_promise_makes_no_task_and_arms_no_timer():
    doc, sends, reply, fx = step(Document(), create("o:a", 100), 0)
    assert reply.status == 200
    assert doc.get("o:a").promise.state == PENDING
    assert doc.get("o:a").task is None
    assert doc.timer_at is None
    assert sends == []
    assert fx == [SetDocument(doc)]


def test_creating_a_targeted_promise_arms_a_retry_timer_and_dispatches():
    doc, sends, _, fx = step(Document(), create("o:a", 100_000, {"resonate:target": W}), 1_000)
    task = doc.get("o:a").task
    assert task.state == T_PENDING
    assert task.version == 0
    assert (task.retry_at, task.lease_at) == (31_000, None)
    # The armed set is the retry timer and the promise deadline; the timer
    # object sits at the nearer of the two.
    assert doc.timer_at == 31_000
    assert sends == [Send(W, Execute("o:a", 0))]
    assert fx == [SetTimeout(31_000), SetDocument(doc), Send(W, Execute("o:a", 0))]


def test_a_promise_created_past_its_deadline_is_born_timed_out():
    doc, sends, reply, _ = step(Document(), create("o:a", 500, {"resonate:target": W}), 900)
    p = doc.get("o:a").promise
    assert p.state == REJECTED_TIMEDOUT
    # Both stamps are the deadline, not `now`.
    assert (p.created_at, p.settled_at) == (500, 500)
    assert doc.get("o:a").task.state == T_FULFILLED
    assert doc.timer_at is None
    assert sends == []
    assert reply.data["promise"]["state"] == "rejected_timedout"


def test_a_timer_promise_created_past_its_deadline_resolves_instead():
    doc, _, _, _ = step(Document(), create("o:a", 500, {TAG_TIMER: "true"}), 900)
    assert doc.get("o:a").promise.state == RESOLVED


def test_create_is_idempotent_on_the_id_alone():
    doc = with_targeted("o:a", 100_000)
    nxt, sends, reply, fx = step(doc, create("o:a", 999_999, {"resonate:target": W}), 1)
    assert reply.status == 200
    # The stored promise wins: the second create's timeout is ignored.
    assert reply.data["promise"]["timeoutAt"] == 100_000
    assert nxt == doc
    assert sends == []
    assert fx == [SetDocument(doc)]


def test_create_rejects_a_target_that_is_not_an_address():
    doc, _, reply, _ = step(Document(), create("o:a", 100, {"resonate:target": "not a url"}), 0)
    assert reply == Reply(400, "Invalid resonate:target address")
    assert doc.objects == []


def test_a_delay_tag_defers_the_first_dispatch_to_its_instant():
    doc, sends, _, _ = step(
        Document(), create("o:a", 100_000, {"resonate:target": W, "resonate:delay": "5000"}), 1_000
    )
    assert doc.get("o:a").task.retry_at == 5_000
    assert sends == [], "a delayed task is not dispatched yet"
    assert doc.timer_at == 5_000


def test_a_delay_already_past_dispatches_immediately():
    doc, sends, _, _ = step(
        Document(), create("o:a", 100_000, {"resonate:target": W, "resonate:delay": "500"}), 1_000
    )
    assert doc.get("o:a").task.retry_at == 31_000
    assert len(sends) == 1


def test_creating_a_promise_settles_an_expired_one_it_names_first():
    # The sweep runs before the operation, so a create on an expired id sees
    # the settled promise and reports it.
    doc = with_targeted("o:a", 1_000)
    nxt, sends, reply, fx = step(doc, create("o:a", 5_000, {"resonate:target": W}), 2_000)
    assert reply.data["promise"]["state"] == "rejected_timedout"
    assert nxt.get("o:a").task.state == T_FULFILLED
    assert nxt.timer_at is None
    # The armed timer was the promise deadline, nearer than the retry.
    assert DelTimeout(1_000) in fx
    assert sends == []


def test_the_timer_moves_when_a_nearer_deadline_arrives():
    doc = with_targeted("o:a", 100_000)  # timer at 30_000
    nxt, _, _, fx = step(doc, create("o:b", 100_000, {"resonate:target": W, "resonate:delay": "10"}), 5)
    assert nxt.timer_at == 10
    assert fx[0] == SetTimeout(10)
    assert DelTimeout(30_000) in fx


# --- the document ----------------------------------------------------------


def test_objects_sort_by_dewey_id():
    doc = Document()
    for id in ["o:10", "o:2", "o:2.1", "o", "o:1"]:
        step_doc, _, _, _ = step(doc, create(id, 100), 0)
        doc = step_doc
    assert [o.id for o in doc.objects] == ["o", "o:1", "o:2", "o:2.1", "o:10"]
    assert dewey("o:2") < dewey("o:10")


def test_ids_that_share_a_key_are_in_order_either_way_round():
    """`o:1` and `o:01` have the same Dewey key, so either may come first.
    The check used to compare against `sorted` of a set, which puts the two
    in hash order: whichever of these documents disagreed with the hash was
    reported unsorted. Found stating the invariant in Lean (`SortedObjs`)."""
    for ids in (["o:1", "o:01"], ["o:01", "o:1"]):
        doc = Document()
        for id in ids:
            doc, _, _, _ = step(doc, create(id, 100), 0)
        assert [o.id for o in doc.objects] == ids


def test_a_timer_armed_at_zero_is_still_armed():
    """A halted task holds no timer. The check read `(retry_at or lease_at)`,
    and 0 is falsy, so a timer at instant 0 passed as no timer at all."""
    doc, _, _, _ = step(Document(), create("o", 100, {"resonate:target": W}), 0)
    doc.objects[0].task = Task(state="halted", retry_at=0)
    doc.timer_at = 0
    assert check_invariants(doc) == "task o: halted with an armed timer"


def test_a_promise_record_carries_the_wire_shape():
    _, _, reply, _ = step(Document(), create("o:a", 100, {"resonate:target": W}), 7)
    assert reply.data == {
        "promise": {
            "id": "o:a",
            "state": "pending",
            "param": {},
            "value": {},
            "tags": {"resonate:target": W},
            "timeoutAt": 100,
            "createdAt": 7,
        }
    }


def test_an_unrelated_request_settles_an_expired_untargeted_promise():
    doc, _, _, _ = step(Document(), create("o:a", 1_000), 0)
    assert doc.timer_at is None, "an untargeted promise arms nothing"
    nxt, sends, _, _ = step(doc, create("o:b", 100_000), 2_000)
    assert nxt.get("o:a").promise.state == REJECTED_TIMEDOUT
    assert nxt.get("o:a").promise.settled_at == 1_000
    assert sends == [], "internal: nothing to fulfil, wake, or notify"


def test_the_sweep_and_the_request_merge_to_one_timer_transition():
    doc = with_targeted("o:a", 1_000)  # timer at 1_000
    # At 2_000 the sweep clears o:a's timer; the request then arms o:b's.
    # The merged effects go straight from 1_000 to o:b's retry, never through
    # "no timer" in between.
    nxt, _, _, fx = step(doc, create("o:b", 100_000, {"resonate:target": W}), 2_000)
    assert fx == [SetTimeout(32_000), SetDocument(nxt), DelTimeout(1_000), Send(W, Execute("o:b", 0))]


# --- the sweep -------------------------------------------------------------


def sweep(doc, now):
    fx = handle_internal(doc, now, CFG)
    docs = [e.doc for e in fx if isinstance(e, SetDocument)]
    assert len(docs) == 1
    assert check_invariants(docs[0]) is None, check_invariants(docs[0])
    sends = [e for e in fx if isinstance(e, Send)]
    before, after = State(doc), State(docs[0], sends)
    assert state_failures(now, before) == [], state_failures(now, before)
    assert state_failures(now, after) == [], state_failures(now, after)
    assert trans_failures(now, before, after) == [], trans_failures(now, before, after)
    assert internal_failures(now, before, after) == [], internal_failures(now, before, after)
    return docs[0], sends, fx


def test_an_empty_document_sweeps_to_nothing():
    nxt, sends, fx = sweep(Document(), 1_000_000)
    assert nxt == Document()
    assert sends == []
    assert fx == [SetDocument(Document())]


def test_a_document_with_nothing_due_is_unchanged():
    doc = with_targeted("o:a", 100_000)
    nxt, sends, _ = sweep(doc, 1_000)
    assert nxt == doc
    assert sends == []


def test_an_expired_promise_settles_at_its_own_deadline():
    doc = with_targeted("o:a", 1_000)
    nxt, sends, fx = sweep(doc, 5_000)
    p = nxt.get("o:a").promise
    assert (p.state, p.settled_at) == (REJECTED_TIMEDOUT, 1_000)
    assert nxt.get("o:a").task.state == T_FULFILLED
    assert nxt.timer_at is None
    assert sends == []
    assert fx == [SetDocument(nxt), DelTimeout(1_000)]


def test_a_pending_task_past_its_retry_is_re_dispatched_at_the_same_version():
    doc = with_targeted("o:a", 100_000)  # retry at 30_000
    nxt, sends, fx = sweep(doc, 30_000)
    assert nxt.get("o:a").task.retry_at == 60_000
    assert sends == [Send(W, Execute("o:a", 0))]
    assert fx[0] == SetTimeout(60_000) and DelTimeout(30_000) in fx


def test_an_expired_lease_hands_the_task_back_pending_at_the_same_version():
    doc = with_targeted("o:a", 100_000)
    t = doc.get("o:a").task
    t.state, t.version, t.pid, t.ttl = T_ACQUIRED, 3, "p1", 5_000
    t.arm_lease(10_000)
    doc.timer_at = 10_000
    nxt, sends, _ = sweep(doc, 10_000)
    t = nxt.get("o:a").task
    assert (t.state, t.version, t.pid, t.ttl) == ("pending", 3, None, None)
    assert (t.retry_at, t.lease_at) == (40_000, None)
    assert sends == [Send(W, Execute("o:a", 3))]


def test_a_sweep_that_fires_nothing_changes_nothing():
    doc = with_targeted("o:a", 100_000)
    for now in (0, 1, 29_999):
        nxt, sends, fx = sweep(doc, now)
        assert nxt == doc and sends == [] and fx == [SetDocument(doc)]


# ===========================================================================
# The remaining operations. Fixtures build documents through the kernel.
# ===========================================================================

from kernel import (  # noqa: E402
    REJECTED, T_HALTED, T_SUSPENDED, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, TaskAcquire, TaskContinue, TaskCreate,
    TaskFence, TaskFulfill, TaskGet, TaskHalt, TaskHeartbeat, TaskRelease,
    TaskSuspend, Unblock,
)

PID = "pid-1"


def apply(doc, req, now):
    doc, _, reply, _ = step(doc, req, now)
    assert reply.status < 400, reply
    return doc


def with_acquired(id, timeout_at=100_000, now=0):
    """A targeted promise whose task has been acquired: version 1, lease armed."""
    doc = with_targeted(id, timeout_at)
    return apply(doc, TaskAcquire(id, 0, PID, 5_000), now)


def with_suspended(task_id, awaited, now=0):
    """`task_id` acquired and then parked on `awaited`, both targeted."""
    doc = with_acquired(task_id)
    doc = apply(doc, create(awaited, 100_000, {"resonate:target": W}), now)
    return apply(doc, TaskSuspend(task_id, 1, (awaited,)), now)


# --- get -------------------------------------------------------------------


def test_getting_an_unknown_promise_is_a_404():
    _, _, reply, _ = step(Document(), PromiseGet("o:a"), 0)
    assert reply == Reply(404, "Promise not found")


def test_getting_an_expired_promise_settles_it_first():
    doc = with_targeted("o:a", 1_000)
    nxt, _, reply, fx = step(doc, PromiseGet("o:a"), 5_000)
    assert reply.data["promise"]["state"] == "rejected_timedout"
    assert nxt.get("o:a").task.state == T_FULFILLED
    assert DelTimeout(1_000) in fx


def test_getting_a_live_promise_changes_nothing():
    doc = with_targeted("o:a", 100_000)
    nxt, sends, reply, fx = step(doc, PromiseGet("o:a"), 1)
    assert reply.status == 200 and nxt == doc and sends == [] and fx == [SetDocument(doc)]


# --- settle ----------------------------------------------------------------


def test_settling_an_unknown_promise_is_a_404():
    _, _, reply, _ = step(Document(), PromiseSettle("o:a", RESOLVED), 0)
    assert reply == Reply(404, "Promise not found")


def test_settling_stamps_now_and_fulfils_the_promises_own_task():
    doc = with_acquired("o:a")
    nxt, sends, reply, fx = step(doc, PromiseSettle("o:a", RESOLVED, Value(data="ok")), 3_000)
    p = nxt.get("o:a").promise
    assert (p.state, p.settled_at, p.value.data) == (RESOLVED, 3_000, "ok")
    assert reply.data["promise"]["settledAt"] == 3_000
    t = nxt.get("o:a").task
    assert (t.state, t.pid, t.ttl, t.retry_at, t.lease_at) == (T_FULFILLED, None, None, None, None)
    assert nxt.timer_at is None and DelTimeout(5_000) in fx
    assert sends == []


def test_settling_twice_reports_the_first_settlement():
    doc = apply(with_acquired("o:a"), PromiseSettle("o:a", RESOLVED, Value(data="first")), 10)
    nxt, _, reply, _ = step(doc, PromiseSettle("o:a", REJECTED, Value(data="second")), 20)
    assert reply.data["promise"]["state"] == "resolved"
    assert reply.data["promise"]["value"] == {"data": "first"}
    assert nxt == doc


def test_settling_unblocks_listeners_and_forgets_them():
    doc = apply(with_targeted("o:a", 100_000), PromiseRegisterListener("o:a", "http://l1"), 0)
    doc = apply(doc, PromiseRegisterListener("o:a", "http://l2"), 0)
    nxt, sends, _, _ = step(doc, PromiseSettle("o:a", RESOLVED), 5)
    record = nxt.get("o:a").promise.to_record("o:a")
    assert sends == [Send("http://l1", Unblock(record)), Send("http://l2", Unblock(record))]
    assert nxt.get("o:a").promise.listeners == []


def test_settling_fans_out_to_awaiters_in_registration_order():
    doc = with_targeted("o:x", 100_000)
    for id in ("o:b", "o:a"):
        doc = apply(doc, create(id, 100_000, {"resonate:target": W}), 0)
        doc = apply(doc, TaskAcquire(id, 0, PID, 5_000), 0)
        doc = apply(doc, TaskSuspend(id, 1, ("o:x",)), 0)
    assert doc.get("o:x").promise.callbacks == ["o:b", "o:a"]
    nxt, sends, _, _ = step(doc, PromiseSettle("o:x", RESOLVED), 10)
    assert sends == [Send(W, Execute("o:b", 1)), Send(W, Execute("o:a", 1))]
    for id in ("o:a", "o:b"):
        t = nxt.get(id).task
        assert (t.state, t.resumes, t.retry_at) == (T_PENDING, {"o:x"}, 30_010)
    assert nxt.get("o:x").promise.callbacks == []


def test_a_settled_awaiter_is_not_resumed():
    doc = with_suspended("o:a", "o:x")
    doc = apply(doc, PromiseSettle("o:a", REJECTED), 5)  # the awaiter settles first
    # The stale registration stays until the awaited settles (the catalogue
    # forbids removing a callback from a pending promise); the fan-out skips
    # the finished awaiter.
    assert doc.get("o:x").promise.callbacks == ["o:a"]
    nxt, sends, _, _ = step(doc, PromiseSettle("o:x", RESOLVED), 6)
    assert sends == [] and nxt.get("o:a").task.state == T_FULFILLED
    assert nxt.get("o:x").promise.callbacks == []


def test_a_halted_awaiter_buffers_a_resume_when_a_settlement_fans_out():
    doc = with_suspended("o:a", "o:x")
    t = doc.get("o:a").task
    t.state = T_HALTED
    nxt, sends, _, _ = step(doc, PromiseSettle("o:x", RESOLVED), 5)
    assert sends == [] and nxt.get("o:a").task.resumes == {"o:x"}
    assert nxt.get("o:a").task.state == T_HALTED


# --- register_callback -----------------------------------------------------


def test_registering_against_an_unknown_awaited_is_a_404():
    doc = with_targeted("o:a", 100_000)
    _, _, reply, _ = step(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    assert reply == Reply(404, "Awaited promise not found")


def test_registering_an_unknown_awaiter_is_a_422():
    doc = with_targeted("o:x", 100_000)
    _, _, reply, _ = step(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    assert reply == Reply(422, "Awaiter promise not found")


def test_an_awaiter_without_a_target_cannot_register():
    doc = apply(with_targeted("o:x", 100_000), create("o:a", 100_000), 0)
    _, _, reply, _ = step(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    assert reply == Reply(422, "Awaiter promise has no resonate:target tag")


def test_an_internal_awaited_is_not_awaitable():
    doc = apply(with_targeted("o:a", 100_000), create("o:x", 100_000), 0)
    _, _, reply, _ = step(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    assert reply == Reply(422, "Awaited promise is not awaitable")


def test_registering_twice_registers_once():
    doc = apply(with_targeted("o:a", 100_000), create("o:x", 100_000, {"resonate:scope": "global"}), 0)
    doc = apply(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    nxt, _, reply, _ = step(doc, PromiseRegisterCallback("o:x", "o:a"), 0)
    assert reply.status == 200 and nxt.get("o:x").promise.callbacks == ["o:a"] and nxt == doc


def test_registering_against_a_settled_promise_registers_nothing():
    """The specification's branch (external.lean:78-83): the caller gets the
    settled record and the store does not change. Not a wake: a suspended task
    always holds a rung on a pending promise, so there is nothing stranded to
    rescue, and a wake here would consume no callback."""
    doc = apply(with_acquired("o:a"), create("o:x", 100_000, {"resonate:scope": "global"}), 0)
    doc = apply(doc, PromiseSettle("o:x", RESOLVED), 5)
    nxt, sends, reply, fx = step(doc, PromiseRegisterCallback("o:x", "o:a"), 6)
    assert reply.data["promise"]["state"] == "resolved"
    t = nxt.get("o:a").task
    assert (t.state, t.resumes) == (T_ACQUIRED, set())
    assert nxt == doc and sends == [] and fx == [SetDocument(doc)]


def test_a_suspended_task_always_holds_a_rung_on_a_pending_promise():
    """Why the branch above can do nothing. Suspension requires every awaited
    promise to be pending, and a settlement drains the callbacks it holds and
    wakes their awaiters, so the two together leave no suspended task waiting
    on something already settled."""
    doc = with_suspended("o:a", "o:x")
    assert doc.get("o:x").promise.callbacks == ["o:a"]
    nxt, sends, _, _ = step(doc, PromiseSettle("o:x", RESOLVED), 5)
    assert nxt.get("o:a").task.state == T_PENDING, "the settle woke it"
    assert sends == [Send(W, Execute("o:a", 1))]


# --- register_listener -----------------------------------------------------


def test_a_listener_address_must_be_an_address():
    _, _, reply, _ = step(with_targeted("o:a", 100_000), PromiseRegisterListener("o:a", "nope"), 0)
    assert reply == Reply(400, "Invalid listener address")


def test_listening_to_an_unknown_promise_is_a_404():
    _, _, reply, _ = step(Document(), PromiseRegisterListener("o:a", "http://l"), 0)
    assert reply == Reply(404, "Awaited promise not found")


def test_listening_to_an_internal_promise_is_a_422():
    doc = apply(Document(), create("o:a", 100_000), 0)
    _, _, reply, _ = step(doc, PromiseRegisterListener("o:a", "http://l"), 0)
    assert reply == Reply(422, "Awaited promise is not awaitable")


def test_listening_to_a_settled_promise_registers_nothing():
    doc = apply(with_targeted("o:a", 100_000), PromiseSettle("o:a", RESOLVED), 1)
    nxt, _, reply, _ = step(doc, PromiseRegisterListener("o:a", "http://l"), 2)
    assert reply.status == 200 and nxt.get("o:a").promise.listeners == []


def test_listening_twice_from_one_address_registers_once():
    doc = apply(with_targeted("o:a", 100_000), PromiseRegisterListener("o:a", "http://l"), 0)
    nxt, _, _, _ = step(doc, PromiseRegisterListener("o:a", "http://l"), 0)
    assert nxt.get("o:a").promise.listeners == ["http://l"]


# --- task.get / task.create ------------------------------------------------


def test_getting_an_unknown_task_is_a_404():
    doc = apply(Document(), create("o:a", 100_000), 0)
    _, _, reply, _ = step(doc, TaskGet("o:a"), 0)
    assert reply == Reply(404, "Task not found")


def test_getting_a_task_whose_promise_expired_reports_it_fulfilled():
    _, _, reply, _ = step(with_targeted("o:a", 1_000), TaskGet("o:a"), 2_000)
    assert reply.data == {"task": {"id": "o:a", "state": "fulfilled", "version": 0, "resumes": 0}}


def task_create(id, timeout_at=100_000, ttl=5_000, tags=None):
    return TaskCreate(PID, ttl, create(id, timeout_at, {"resonate:target": W, **(tags or {})}))


def test_task_create_hands_back_an_already_acquired_task():
    doc, sends, reply, fx = step(Document(), task_create("o:a"), 1_000)
    t = doc.get("o:a").task
    assert (t.state, t.version, t.pid, t.ttl, t.lease_at, t.retry_at) == (T_ACQUIRED, 1, PID, 5_000, 6_000, None)
    assert sends == [], "the caller is the worker: no dispatch"
    assert reply.data["task"]["version"] == 1 and reply.data["preload"] == []
    assert fx[0] == SetTimeout(6_000)


def test_task_create_past_the_deadline_hands_back_a_fulfilled_task():
    doc, _, reply, _ = step(Document(), task_create("o:a", timeout_at=500), 900)
    assert doc.get("o:a").task.state == T_FULFILLED
    assert reply.data["promise"]["state"] == "rejected_timedout" and doc.timer_at is None


def test_task_create_claims_a_pending_task_and_bumps_its_version():
    doc = with_targeted("o:a", 100_000)
    nxt, sends, reply, _ = step(doc, task_create("o:a"), 10)
    t = nxt.get("o:a").task
    assert (t.state, t.version, t.lease_at) == (T_ACQUIRED, 1, 5_010) and sends == []


def test_task_create_on_a_claimed_task_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), task_create("o:a"), 10)
    assert reply == Reply(409, "Already exists")


def test_task_create_on_a_fulfilled_task_reports_it_without_preload():
    doc = apply(with_acquired("o:a"), PromiseSettle("o:a", RESOLVED), 5)
    _, _, reply, _ = step(doc, task_create("o:a"), 10)
    assert reply.data["task"]["state"] == "fulfilled" and reply.data["preload"] == []


def test_task_create_on_a_promise_without_a_task_is_a_422():
    doc = apply(Document(), create("o:a", 100_000), 0)
    _, _, reply, _ = step(doc, task_create("o:a"), 0)
    assert reply == Reply(422, "The promise does not have a resonate:target tag")


# --- task.acquire ----------------------------------------------------------


def test_acquiring_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), TaskAcquire("o:a", 0, PID, 5_000), 0)
    assert reply == Reply(404, "Task not found")


def test_acquiring_a_task_that_is_not_pending_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), TaskAcquire("o:a", 1, PID, 5_000), 0)
    assert reply == Reply(409, "Task is not pending")


def test_acquiring_at_the_wrong_version_is_a_conflict():
    _, _, reply, _ = step(with_targeted("o:a", 100_000), TaskAcquire("o:a", 7, PID, 5_000), 0)
    assert reply == Reply(409, "Version mismatch")


def test_acquiring_takes_the_lease_and_drops_buffered_resumes():
    doc = apply(with_targeted("o:a", 100_000), create("o:x", 100_000), 0)
    doc.get("o:a").task.resumes.add("o:x")
    nxt, sends, reply, fx = step(doc, TaskAcquire("o:a", 0, PID, 5_000), 100)
    t = nxt.get("o:a").task
    assert (t.state, t.version, t.pid, t.ttl, t.resumes, t.lease_at, t.retry_at) == (
        T_ACQUIRED, 1, PID, 5_000, set(), 5_100, None)
    assert reply.data["task"] == {"id": "o:a", "state": "acquired", "version": 1, "resumes": 0, "ttl": 5_000, "pid": PID}
    assert fx[0] == SetTimeout(5_100) and DelTimeout(30_000) in fx and sends == []


# --- task.release ----------------------------------------------------------


def test_releasing_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), TaskRelease("o:a", 0), 0)
    assert reply == Reply(404, "Task not found")


def test_releasing_at_the_wrong_version_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), TaskRelease("o:a", 0), 0)
    assert reply == Reply(409, "Task version mismatch or invalid state")


def test_releasing_re_dispatches_at_the_same_version():
    nxt, sends, reply, _ = step(with_acquired("o:a"), TaskRelease("o:a", 1), 100)
    t = nxt.get("o:a").task
    assert (t.state, t.version, t.pid, t.ttl, t.retry_at, t.lease_at) == (T_PENDING, 1, None, None, 30_100, None)
    assert sends == [Send(W, Execute("o:a", 1))] and reply == Reply(200, {})


# --- task.fulfill ----------------------------------------------------------


def test_fulfilling_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), TaskFulfill("o:a", 1, PromiseSettle("o:a", RESOLVED)), 0)
    assert reply == Reply(404, "Task not found")


def test_fulfilling_at_the_wrong_version_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), TaskFulfill("o:a", 2, PromiseSettle("o:a", RESOLVED)), 0)
    assert reply == Reply(409, "Task version mismatch or invalid state")


def test_fulfilling_settles_the_promise_and_runs_the_chain():
    doc = apply(with_acquired("o:a"), PromiseRegisterListener("o:a", "http://l"), 0)
    nxt, sends, reply, _ = step(doc, TaskFulfill("o:a", 1, PromiseSettle("o:a", RESOLVED, Value(data="v"))), 9)
    assert reply.data["promise"]["state"] == "resolved" and reply.data["promise"]["settledAt"] == 9
    assert nxt.get("o:a").task.state == T_FULFILLED
    assert len(sends) == 1 and isinstance(sends[0].msg, Unblock)


# --- task.suspend ----------------------------------------------------------


def test_suspending_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), TaskSuspend("o:a", 1, ("o:x",)), 0)
    assert reply == Reply(404, "Task not found")


def test_suspending_at_the_wrong_version_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), TaskSuspend("o:a", 0, ("o:x",)), 0)
    assert reply == Reply(409, "Task is not acquired or version mismatch")


def test_suspending_on_a_missing_promise_is_a_422():
    _, _, reply, _ = step(with_acquired("o:a"), TaskSuspend("o:a", 1, ("o:x",)), 0)
    assert reply == Reply(422, "Awaited promise not found")


def test_suspending_on_an_internal_promise_is_a_422():
    doc = apply(with_acquired("o:a"), create("o:x", 100_000), 0)
    _, _, reply, _ = step(doc, TaskSuspend("o:a", 1, ("o:x",)), 0)
    assert reply == Reply(422, "Awaited promise is not awaitable")


def test_suspending_parks_the_task_and_registers_each_awaited_once():
    doc = with_acquired("o:a")
    for x in ("o:x", "o:y"):
        doc = apply(doc, create(x, 100_000, {"resonate:target": W}), 0)
    nxt, sends, reply, fx = step(doc, TaskSuspend("o:a", 1, ("o:x", "o:y")), 10)
    t = nxt.get("o:a").task
    assert (t.state, t.pid, t.ttl, t.retry_at, t.lease_at) == (T_SUSPENDED, None, None, None, None)
    assert nxt.get("o:x").promise.callbacks == ["o:a"] and nxt.get("o:y").promise.callbacks == ["o:a"]
    assert reply == Reply(200, {}) and sends == []
    assert DelTimeout(5_000) in fx and nxt.timer_at == 30_000  # the awaited tasks' retries remain


def test_suspending_on_an_already_settled_promise_tells_the_caller_to_carry_on():
    doc = apply(with_acquired("o:a"), create("o:x", 100_000, {"resonate:scope": "global", "resonate:branch": "o"}), 0)
    doc = apply(doc, PromiseSettle("o:x", RESOLVED), 1)
    nxt, _, reply, _ = step(doc, TaskSuspend("o:a", 1, ("o:x",)), 2)
    assert reply.status == 300 and reply.data == {"preload": []}
    assert nxt.get("o:a").task.state == T_ACQUIRED, "the task keeps running"


def test_suspending_drops_the_resumes_a_previous_run_buffered():
    doc = apply(with_acquired("o:a"), create("o:x", 100_000, {"resonate:scope": "global"}), 0)
    doc.get("o:a").task.resumes.add("o:x")
    nxt, _, _, _ = step(doc, TaskSuspend("o:a", 1, ("o:x",)), 1)
    assert nxt.get("o:a").task.resumes == set()


# --- task.fence ------------------------------------------------------------


def fence(action, version=1, corr="c1", id="o:a"):
    return TaskFence(id, version, corr, action)


def test_fencing_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), fence(create("o:b", 100)), 0)
    assert reply == Reply(404, "Task not found")


def test_fencing_at_the_wrong_version_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), fence(create("o:b", 100), version=2), 0)
    assert reply == Reply(409, "Version mismatch")


def test_a_fenced_create_returns_a_nested_envelope():
    nxt, sends, reply, _ = step(with_acquired("o:a"), fence(create("o:a:1", 100_000, {"resonate:target": W})), 3)
    assert reply.status == 200
    assert reply.data["action"]["kind"] == "promise.create"
    assert reply.data["action"]["head"] == {"corrId": "c1", "status": 200, "version": "2026-04-01"}
    assert reply.data["action"]["data"]["promise"]["id"] == "o:a:1"
    assert reply.data["preload"] == []
    assert nxt.get("o:a:1").task.state == T_PENDING and sends == [Send(W, Execute("o:a:1", 0))]


def test_a_fenced_create_with_a_bad_target_is_a_top_level_400():
    _, _, reply, _ = step(with_acquired("o:a"), fence(create("o:a:1", 100, {"resonate:target": "nope"})), 3)
    assert reply == Reply(400, "Invalid resonate:target address")


def test_a_fenced_settle_of_a_missing_promise_reports_404_inside_a_200():
    _, _, reply, _ = step(with_acquired("o:a"), fence(PromiseSettle("o:zz", RESOLVED)), 3)
    assert reply.status == 200
    assert reply.data["action"]["head"]["status"] == 404
    assert reply.data["action"]["data"] == "Promise not found"


def test_a_fenced_settle_runs_the_settlement_chain():
    doc = apply(with_acquired("o:a"), create("o:a:1", 100_000, {"resonate:target": W}), 0)
    doc = apply(doc, PromiseRegisterListener("o:a:1", "http://l"), 0)
    nxt, sends, reply, _ = step(doc, fence(PromiseSettle("o:a:1", RESOLVED)), 4)
    assert reply.data["action"]["data"]["promise"]["state"] == "resolved"
    assert nxt.get("o:a:1").task.state == T_FULFILLED
    assert len(sends) == 1 and isinstance(sends[0].msg, Unblock)


# --- task.heartbeat --------------------------------------------------------


def test_a_heartbeat_extends_the_lease_of_a_task_the_caller_owns():
    nxt, _, reply, fx = step(with_acquired("o:a"), TaskHeartbeat(PID, (("o:a", 1),)), 3_000)
    assert nxt.get("o:a").task.lease_at == 8_000 and reply == Reply(200, {})
    assert fx[0] == SetTimeout(8_000) and DelTimeout(5_000) in fx


def test_a_heartbeat_from_another_process_changes_nothing():
    doc = with_acquired("o:a")
    nxt, _, _, fx = step(doc, TaskHeartbeat("someone-else", (("o:a", 1),)), 3_000)
    assert nxt == doc and fx == [SetDocument(doc)]


def test_a_heartbeat_at_a_stale_version_changes_nothing():
    doc = with_acquired("o:a")
    nxt, _, _, _ = step(doc, TaskHeartbeat(PID, (("o:a", 0),)), 3_000)
    assert nxt == doc


def test_a_heartbeat_for_an_unknown_task_is_still_a_200():
    _, _, reply, _ = step(Document(), TaskHeartbeat(PID, (("o:a", 1),)), 0)
    assert reply == Reply(200, {})


# --- task.halt / task.continue ---------------------------------------------


def test_halting_an_unknown_task_is_a_404():
    _, _, reply, _ = step(Document(), TaskHalt("o:a"), 0)
    assert reply == Reply(404, "Task not found")


def test_halting_disarms_the_task():
    nxt, _, reply, fx = step(with_acquired("o:a"), TaskHalt("o:a"), 0)
    t = nxt.get("o:a").task
    assert (t.state, t.pid, t.ttl, t.retry_at, t.lease_at) == (T_HALTED, None, None, None, None)
    assert reply == Reply(200, {}) and DelTimeout(5_000) in fx


def test_halting_twice_is_idempotent():
    doc = apply(with_acquired("o:a"), TaskHalt("o:a"), 0)
    nxt, _, reply, fx = step(doc, TaskHalt("o:a"), 1)
    assert reply == Reply(200, {}) and nxt == doc and fx == [SetDocument(doc)]


def test_halting_a_finished_task_is_a_conflict():
    doc = apply(with_acquired("o:a"), PromiseSettle("o:a", RESOLVED), 1)
    _, _, reply, _ = step(doc, TaskHalt("o:a"), 2)
    assert reply == Reply(409, "Task is fulfilled")


def test_continuing_a_task_that_is_not_halted_is_a_conflict():
    _, _, reply, _ = step(with_acquired("o:a"), TaskContinue("o:a"), 0)
    assert reply == Reply(409, "Task is not halted")


def test_continuing_re_dispatches_a_halted_task():
    doc = apply(with_acquired("o:a"), TaskHalt("o:a"), 0)
    nxt, sends, reply, fx = step(doc, TaskContinue("o:a"), 50)
    t = nxt.get("o:a").task
    assert (t.state, t.version, t.retry_at) == (T_PENDING, 1, 30_050)
    assert sends == [Send(W, Execute("o:a", 1))] and fx[0] == SetTimeout(30_050)


# --- preload ---------------------------------------------------------------


def test_preload_is_the_rest_of_the_branch_in_dewey_order():
    doc = Document()
    for id in ("o:a:10", "o:a:2", "o:a:1"):
        doc = apply(doc, create(id, 100_000, {"resonate:branch": "o:a"}), 0)
    doc = apply(doc, create("o:b", 100_000, {"resonate:branch": "o:b"}), 0)
    doc = apply(doc, create("o:a", 100_000, {"resonate:target": W, "resonate:branch": "o:a"}), 0)
    _, _, reply, _ = step(doc, TaskAcquire("o:a", 0, PID, 5_000), 1)
    assert [p["id"] for p in reply.data["preload"]] == ["o:a:1", "o:a:2", "o:a:10"]


def test_preload_is_truncated_at_the_limit():
    doc = Document()
    for i in range(1, 15):
        doc = apply(doc, create(f"o:a:{i}", 100_000, {"resonate:branch": "o:a"}), 0)
    doc = apply(doc, create("o:a", 100_000, {"resonate:target": W, "resonate:branch": "o:a"}), 0)
    _, _, reply, _ = step(doc, TaskAcquire("o:a", 0, PID, 5_000), 1)
    assert len(reply.data["preload"]) == CFG.preload_limit == 10


def test_a_promise_without_a_branch_preloads_nothing():
    _, _, reply, _ = step(with_targeted("o:a", 100_000), TaskAcquire("o:a", 0, PID, 5_000), 1)
    assert reply.data["preload"] == []


# --- the whole loop --------------------------------------------------------


def test_a_remote_call_end_to_end():
    """Post 002: create here, settle over there, resume, run from the top."""
    doc = with_targeted("run", 100_000)                                   # the run itself
    doc = apply(doc, TaskAcquire("run", 0, "w1", 5_000), 1)                # a worker claims it
    doc = apply(doc, fence(create("run:1", 100_000, {"resonate:target": W}), corr="c", id="run"), 2)  # durable() creates the rpc's promise
    doc = apply(doc, TaskSuspend("run", 1, ("run:1",)), 3)                # Blocked: unwind, park
    assert doc.get("run").task.state == T_SUSPENDED and doc.timer_at == 30_002
    doc = apply(doc, TaskAcquire("run:1", 0, "w2", 5_000), 4)              # another worker takes the callee
    nxt, sends, _, _ = step(doc, TaskFulfill("run:1", 1, PromiseSettle("run:1", RESOLVED, Value(data="42"))), 5)
    assert sends == [Send(W, Execute("run", 1))]                          # the settle wakes the caller
    t = nxt.get("run").task
    assert (t.state, t.version, t.resumes) == (T_PENDING, 1, {"run:1"})
    doc = apply(nxt, TaskAcquire("run", 1, "w3", 5_000), 6)                # any worker resumes it
    _, _, reply, _ = step(doc, fence(create("run:1", 0), version=2, corr="c", id="run"), 7)  # replay: create is a read
    assert reply.data["action"]["data"]["promise"]["value"] == {"data": "42"}
    nxt, _, _, _ = step(doc, TaskFulfill("run", 2, PromiseSettle("run", RESOLVED, Value(data="done"))), 8)
    assert nxt.get("run").promise.state == RESOLVED and nxt.timer_at is None


def test_settling_after_the_lease_expired_reclaims_the_task_first():
    # The sweep runs before the request: a lease past due hands the task back
    # and would re-dispatch it, but the settle then fulfils it in the same
    # step, so the merge drops the dispatch the request overtook.
    doc = with_acquired("o:a")  # lease at 5_000
    nxt, sends, _, _ = step(doc, PromiseSettle("o:a", RESOLVED), 7_000)
    assert sends == []
    assert nxt.get("o:a").task.state == T_FULFILLED


def test_acquiring_after_the_lease_expired_drops_the_stale_dispatch():
    # Lease past due: the sweep hands the task back at version 1; the acquire
    # takes it at version 2. The version-1 execute could only be refused.
    doc = with_acquired("o:a")
    nxt, sends, reply, _ = step(doc, TaskAcquire("o:a", 1, "p2", 5_000), 7_000)
    assert reply.status == 200 and nxt.get("o:a").task.version == 2
    assert sends == []
