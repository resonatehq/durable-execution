"""Ported from crates/resonate-server-blob/src/kernel/handle.rs, `mod tests`."""

from kernel import (
    PENDING, REJECTED_TIMEDOUT, RESOLVED, T_FULFILLED, T_PENDING, TAG_TIMER,
    DelTimeout, Document, Execute, KernelCfg, PromiseCreate, Reply, Send,
    SetDocument, SetTimeout, Value, check_invariants, dewey, handle_external,
)

W = "http://worker:9999"
CFG = KernelCfg(retry_timeout=30_000)


def step(doc, req, now):
    """Apply a request: the new document, the sends, and the reply."""
    fx, reply = handle_external(doc, req, now, CFG)
    docs = [e.doc for e in fx if isinstance(e, SetDocument)]
    assert len(docs) == 1
    assert check_invariants(docs[0]) is None, check_invariants(docs[0])
    sends = [e for e in fx if isinstance(e, Send)]
    return docs[0], sends, reply, fx


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
    # Ghost timeouts: the id a request names is swept before the operation.
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
