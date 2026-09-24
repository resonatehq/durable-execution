"""The catalogue, held to two standards the specification sets for itself.

1. Every property must be FALSIFIABLE: a hand-built violator is rejected by
   exactly the entry that names it. A property that cannot fail is not being
   checked.
2. The corpus must REACH the states that make each guard bite: a randomized
   walk over an adversarial alphabet, with the whole catalogue evaluated at
   every state and every consecutive pair, and a tally proving the
   interesting transitions actually happened.
"""

import copy
from types import SimpleNamespace

from resonate.testing import properties as P
from resonate.kernel import (
    Document, KernelCfg, Object, PENDING, Promise, REJECTED,
    REJECTED_TIMEDOUT, RESOLVED, Send, SetDocument, T_ACQUIRED, T_FULFILLED,
    T_HALTED, T_PENDING, T_SUSPENDED, Task, check_invariants, handle_external,
    handle_internal,
)
from resonate.types import (
    Execute, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, TaskAcquire, TaskContinue,
    TaskCreate, TaskFence, TaskFulfill, TaskGet, TaskHalt, TaskHeartbeat,
    TaskRelease, TaskSuspend, Unblock, Value,
)

W = "http://w"
CFG = KernelCfg(retry_timeout=30_000)


def run(doc, req, now):
    fx, reply = handle_external(doc, req, now, CFG)
    doc = next(e.doc for e in fx if isinstance(e, SetDocument))
    return doc, [e for e in fx if isinstance(e, Send)], reply


def apply(doc, req, now):
    doc, _, reply = run(doc, req, now)
    assert reply.status < 400, reply
    return doc


def create(id, timeout_at, tags=None):
    return PromiseCreate(id, timeout_at, Value(), tags or {})


NOW = 100


def base():
    """A legal state at NOW with one of everything: an acquired task (o:a), a
    pending one (o:b), an internal promise (o:c), a settled promise with a
    fulfilled task (o:d), an awaitable promise with a listener (o:e), and a
    task suspended on it (o:s)."""
    d = Document()
    d = apply(d, create("o:a", 100_000, {"resonate:target": W}), 0)
    d = apply(d, TaskAcquire("o:a", 0, "p1", 5_000), 10)
    d = apply(d, create("o:b", 100_000, {"resonate:target": W}), 20)
    d = apply(d, create("o:c", 100_000), 30)
    d = apply(d, create("o:d", 100_000, {"resonate:target": W}), 40)
    d = apply(d, PromiseSettle("o:d", RESOLVED), 50)
    d = apply(d, create("o:e", 100_000, {"resonate:scope": "global"}), 60)
    d = apply(d, PromiseRegisterListener("o:e", "http://l"), 70)
    d = apply(d, create("o:s", 100_000, {"resonate:target": W}), 80)
    d = apply(d, TaskAcquire("o:s", 0, "p2", 5_000), 85)
    d = apply(d, TaskSuspend("o:s", 1, ("o:e",)), 90)
    return P.State(d, [Send(W, Execute("o:b", 0)), Send(W, Execute("o:s", 0))])


def test_the_base_state_is_legal():
    s = base()
    assert P.state_failures(NOW, s) == []
    assert P.trans_failures(NOW, s, s) == []
    assert P.internal_failures(NOW, s, s) == []


def mut(s, f):
    s = copy.deepcopy(s)
    f(s)
    return s


def p(s, id):
    return s.doc.get(id).promise


def t(s, id):
    return s.doc.get(id).task


def settle(s, id, state, at):
    p(s, id).state, p(s, id).settled_at = state, at


def fulfil(s, id):
    t(s, id).state = T_FULFILLED
    t(s, id).pid = t(s, id).ttl = t(s, id).retry_at = t(s, id).lease_at = None
    t(s, id).resumes = set()


def sched(**kw):
    return SimpleNamespace(**{"id": "s", "promise_tags": {}, "created_at": 0, "next_run_at": 10, "last_run_at": None, **kw})


def record(s, id):
    return p(s, id).to_record(id)


# --- state violators -------------------------------------------------------
# One per entry. Each mutation is meant to break exactly the named entry, but
# a violator may trip neighbours too; the assertion is that the NAMED entry
# rejects it.

STATE_VIOLATORS = {
    "well_formed_promise_created_at_lte_timeout_at": lambda s: setattr(p(s, "o:c"), "created_at", 200_000),
    "well_formed_promise_pending_created_before_deadline": lambda s: setattr(p(s, "o:c"), "created_at", 100_000),
    "well_formed_promise_settled_at_lte_timeout_at": lambda s: setattr(p(s, "o:d"), "settled_at", 200_000),
    "well_formed_promise_created_at_lte_settled_at": lambda s: setattr(p(s, "o:d"), "settled_at", 10),
    "well_formed_promise_settled_at_iff_not_pending": lambda s: setattr(p(s, "o:c"), "settled_at", 5),
    "well_formed_promise_pending_has_no_value": lambda s: setattr(p(s, "o:c"), "value", Value(data="x")),
    "well_formed_promise_deadline_verdict_matches_timer_tag": lambda s: setattr(p(s, "o:d"), "settled_at", 100_000),
    "well_formed_promise_deadline_settlement_has_no_value": lambda s: (
        settle(s, "o:d", REJECTED_TIMEDOUT, 100_000), setattr(p(s, "o:d"), "value", Value(data="x"))),
    "well_formed_promise_timer_not_targeted": lambda s: p(s, "o:b").tags.__setitem__("resonate:timer", "true"),
    "well_formed_promise_timedout_is_server_owned": lambda s: setattr(p(s, "o:d"), "state", REJECTED_TIMEDOUT),
    "well_formed_promise_callbacks_unique": lambda s: setattr(p(s, "o:e"), "callbacks", ["o:s", "o:s"]),
    "well_formed_promise_listeners_unique": lambda s: setattr(p(s, "o:e"), "listeners", ["http://l", "http://l"]),
    "well_formed_promise_obligations_require_external": lambda s: setattr(p(s, "o:c"), "listeners", ["http://l"]),
    "well_formed_promise_awaiter_is_not_self": lambda s: p(s, "o:e").callbacks.append("o:e"),
    "well_formed_promise_callbacks_same_origin": lambda s: p(s, "o:e").callbacks.append("x:1"),
    "well_formed_promise_created_at_lte_now": lambda s: setattr(p(s, "o:c"), "created_at", 1_000),
    "well_formed_promise_settled_at_lte_now": lambda s: setattr(p(s, "o:d"), "settled_at", 1_000),
    "well_formed_task_acquired_iff_has_pid": lambda s: setattr(t(s, "o:a"), "pid", None),
    "well_formed_task_acquired_iff_has_ttl": lambda s: setattr(t(s, "o:a"), "ttl", None),
    "well_formed_task_acquired_iff_has_lease_timeout_at": lambda s: setattr(t(s, "o:a"), "lease_at", None),
    "well_formed_task_pending_iff_has_retry_timeout_at": lambda s: setattr(t(s, "o:b"), "retry_at", None),
    "well_formed_task_fulfilled_is_cleared": lambda s: setattr(t(s, "o:d"), "pid", "p"),
    "well_formed_task_suspended_is_cleared": lambda s: setattr(t(s, "o:s"), "ttl", 5),
    "well_formed_task_halted_is_cleared": lambda s: (setattr(t(s, "o:s"), "state", T_HALTED), setattr(t(s, "o:s"), "pid", "p")),
    "well_formed_task_suspended_has_no_resumes": lambda s: setattr(t(s, "o:s"), "resumes", {"o:e"}),
    "well_formed_task_acquired_version_positive": lambda s: setattr(t(s, "o:a"), "version", 0),
    "well_formed_schedule_promise_tags_not_timer_targeted": lambda s: setattr(
        s, "schedules", (sched(promise_tags={"resonate:timer": "true", "resonate:target": W}),)),
    "well_formed_schedule_created_at_lte_next_run_at": lambda s: setattr(s, "schedules", (sched(created_at=20, next_run_at=10),)),
    "well_formed_schedule_created_at_lte_last_run_at": lambda s: setattr(s, "schedules", (sched(created_at=20, last_run_at=5, next_run_at=30),)),
    "well_formed_schedule_last_run_at_lt_next_run_at": lambda s: setattr(s, "schedules", (sched(last_run_at=10, next_run_at=10),)),
    "well_formed_store_object_ids_unique": lambda s: s.doc.objects.append(copy.deepcopy(s.doc.get("o:c"))),
    "well_formed_store_schedule_ids_unique": lambda s: setattr(s, "schedules", (sched(), sched())),
    "well_formed_store_outbox_keys_unique": lambda s: s.outbox.append(Send(W, Execute("o:b", 0))),
    "consistent_task_iff_kind_task": lambda s: setattr(s.doc.get("o:c"), "task", Task(T_PENDING, 0, retry_at=1)),
    "consistent_settled_promise_has_fulfilled_task": lambda s: (setattr(t(s, "o:d"), "state", T_PENDING), setattr(t(s, "o:d"), "retry_at", 1)),
    "consistent_callback_awaiter_is_targeted": lambda s: setattr(p(s, "o:e"), "callbacks", ["o:c"]),
    "consistent_outbox_execute_names_existing_task": lambda s: s.outbox.append(Send(W, Execute("o:zz", 0))),
    "consistent_outbox_never_ahead": lambda s: s.outbox.append(Send(W, Execute("o:a", 5))),
    "consistent_outbox_execute_address_is_target_tag": lambda s: s.outbox.append(Send("http://other", Execute("o:a", 1))),
    "consistent_outbox_unblock_names_settled_promise": lambda s: s.outbox.append(Send("http://l", Unblock(record(s, "o:c")))),
    "consistent_settled_task_promise_settled": lambda s: fulfil(s, "o:b"),
    "consistent_suspended_task_holds_rung": lambda s: setattr(p(s, "o:e"), "callbacks", []),
    "well_formed_task_ttl_positive": lambda s: setattr(t(s, "o:a"), "ttl", 0),
    "well_formed_promise_target_is_nonempty": lambda s: p(s, "o:b").tags.__setitem__("resonate:target", ""),
    "well_formed_promise_delay_before_deadline": lambda s: p(s, "o:b").tags.__setitem__("resonate:delay", "999999999"),
}

#: Structurally unfalsifiable here: `resumes` is a set, so it cannot hold a
#: duplicate. The entry is kept in the catalogue so the count is the spec's.
STRUCTURAL = {"well_formed_task_resumes_unique"}


def test_every_state_property_has_a_violator():
    names = {n for n, _ in P.STATE + P.GAPS}
    assert names == set(STATE_VIOLATORS) | STRUCTURAL, names ^ (set(STATE_VIOLATORS) | STRUCTURAL)


def test_each_state_violator_is_rejected_by_its_own_entry():
    s = base()
    for name, violate in STATE_VIOLATORS.items():
        assert name in P.state_failures(NOW, mut(s, violate)), name


# --- transition violators --------------------------------------------------


def new_obj(s, id, tags, task=None):
    s.doc.insert(Object(id, Promise(PENDING, Value(), Value(), tags, 100_000, NOW), task))


TRANS_VIOLATORS = {
    "preserved_promise_birth_fields_immutable": lambda a, b: setattr(p(b, "o:c"), "timeout_at", 100_001),
    "preserved_settled_promise_record": lambda a, b: setattr(p(b, "o:d"), "value", Value(data="x")),
    "monotone_promise_set_grows": lambda a, b: b.doc.objects.pop(),
    "monotone_task_set_grows": lambda a, b: setattr(b.doc.get("o:b"), "task", None),
    "monotone_task_version_increases_only_on_acquisition": lambda a, b: setattr(t(b, "o:b"), "version", 1),
    "preserved_fulfilled_task": lambda a, b: setattr(t(b, "o:d"), "version", 7),
    "preserved_promise_state_frozen_once_settled": lambda a, b: setattr(p(b, "o:d"), "state", REJECTED),
    "preserved_promise_settlement_is_one_way": lambda a, b: settle(b, "o:d", PENDING, None),
    "consistent_promise_settled_at_moves_with_state": lambda a, b: setattr(p(b, "o:c"), "settled_at", 5),
    "preserved_promise_value_until_settlement": lambda a, b: setattr(p(b, "o:c"), "value", Value(data="x")),
    "preserved_promise_no_duplicate_ids": lambda a, b: b.doc.objects.append(copy.deepcopy(b.doc.get("o:c"))),
    "monotone_promise_callbacks_grow_while_pending": lambda a, b: setattr(p(b, "o:e"), "callbacks", []),
    "monotone_promise_callbacks_shrink_once_settled": lambda a, b: (
        settle(b, "o:e", RESOLVED, NOW), setattr(p(b, "o:e"), "callbacks", ["o:s", "o:b"])),
    "monotone_promise_listeners_grow_while_pending": lambda a, b: setattr(p(b, "o:e"), "listeners", []),
    "monotone_promise_listeners_shrink_once_settled": lambda a, b: (
        settle(b, "o:e", RESOLVED, NOW), setattr(p(b, "o:e"), "listeners", ["http://l", "http://m"])),
    "consistent_promise_state_edge_admissible": lambda a, b: setattr(p(b, "o:d"), "state", REJECTED),
    "consistent_task_state_edge_admissible": lambda a, b: (setattr(t(b, "o:d"), "state", T_PENDING), setattr(t(b, "o:d"), "retry_at", 1)),
    "preserved_task_acquisition_only_from_pending": lambda a, b: (
        setattr(t(b, "o:s"), "state", T_ACQUIRED), setattr(t(b, "o:s"), "pid", "p"), setattr(t(b, "o:s"), "ttl", 1), setattr(t(b, "o:s"), "lease_at", NOW + 1)),
    "preserved_task_suspension_only_from_acquired": lambda a, b: (setattr(t(b, "o:b"), "state", T_SUSPENDED), setattr(t(b, "o:b"), "retry_at", None)),
    "preserved_task_halted_only_reenters_via_pending": lambda a, b: (
        setattr(t(a, "o:s"), "state", T_HALTED),
        setattr(t(b, "o:s"), "state", T_ACQUIRED), setattr(t(b, "o:s"), "pid", "p"), setattr(t(b, "o:s"), "ttl", 1), setattr(t(b, "o:s"), "lease_at", NOW + 1)),
    "consistent_settlement_fulfils_task": lambda a, b: settle(b, "o:b", RESOLVED, NOW),
    "consistent_task_fulfilment_needs_settlement": lambda a, b: fulfil(b, "o:b"),
    "consistent_obligation_discharge_requires_settled": lambda a, b: setattr(p(b, "o:e"), "listeners", []),
    "consistent_callback_consumption_resumes_awaiter": lambda a, b: (settle(b, "o:e", RESOLVED, NOW), setattr(p(b, "o:e"), "callbacks", [])),
    "consistent_listener_consumption_enqueues_unblock": lambda a, b: (settle(b, "o:e", RESOLVED, NOW), setattr(p(b, "o:e"), "listeners", [])),
    "consistent_wake_follows_callback_consumption": lambda a, b: (
        setattr(t(b, "o:s"), "state", T_PENDING), setattr(t(b, "o:s"), "retry_at", NOW + 30_000), setattr(t(b, "o:s"), "resumes", {"o:e"})),
    "consistent_suspension_registers_callback_adapted": lambda a, b: (
        setattr(t(a, "o:b"), "state", T_ACQUIRED), setattr(t(a, "o:b"), "pid", "p"), setattr(t(a, "o:b"), "ttl", 1), setattr(t(a, "o:b"), "lease_at", NOW + 1), setattr(t(a, "o:b"), "retry_at", None),
        setattr(t(b, "o:b"), "state", T_SUSPENDED), setattr(t(b, "o:b"), "retry_at", None)),
    "consistent_task_birth_couples_promise_birth": lambda a, b: new_obj(b, "o:n", {"resonate:target": W}),
    "monotone_outbox_keys_never_disappear": lambda a, b: b.outbox.clear(),
    "consistent_new_execute_matches_task_and_target": lambda a, b: b.outbox.append(Send(W, Execute("o:b", 3))),
    "consistent_new_unblock_carries_stored_record": lambda a, b: b.outbox.append(Send("http://x", Unblock({**record(b, "o:d"), "value": {"data": "wrong"}}))),
    "consistent_new_unblock_discharges_its_listener": lambda a, b: b.outbox.append(Send("http://nobody", Unblock(record(b, "o:d")))),
    "consistent_task_birth_state": lambda a, b: new_obj(b, "o:n", {"resonate:target": W}, Task(T_PENDING, 0)),
    "consistent_task_lease_released_atomically": lambda a, b: (setattr(t(b, "o:a"), "state", T_PENDING), setattr(t(b, "o:a"), "retry_at", NOW + 30_000), setattr(t(b, "o:a"), "lease_at", None)),
    "preserved_task_lease_holder_stable": lambda a, b: setattr(t(b, "o:a"), "pid", "p9"),
    "consistent_task_lease_fields_move_together": lambda a, b: setattr(t(b, "o:a"), "ttl", 6_000),
    "monotone_task_resumes_grow_or_clear": lambda a, b: (setattr(t(a, "o:a"), "resumes", {"o:e", "o:c"}), setattr(t(b, "o:a"), "resumes", {"o:e"})),
    "consistent_task_resumes_cleared_only_on_dispatch_or_park": lambda a, b: (
        setattr(t(a, "o:a"), "resumes", {"o:e"}), setattr(t(b, "o:a"), "resumes", set()),
        setattr(t(b, "o:a"), "state", T_PENDING), setattr(t(b, "o:a"), "pid", None), setattr(t(b, "o:a"), "ttl", None),
        setattr(t(b, "o:a"), "lease_at", None), setattr(t(b, "o:a"), "retry_at", NOW + 30_000)),
    "preserved_no_dead_dispatch": lambda a, b: (setattr(t(b, "o:d"), "state", T_PENDING), setattr(t(b, "o:d"), "retry_at", NOW + 30_000)),
    "preserved_execute_only_for_live_task": lambda a, b: b.outbox.append(Send(W, Execute("o:d", 0))),
    "consistent_promise_settlement_stamp": lambda a, b: settle(b, "o:c", RESOLVED, 5),
    "preserved_timedout_is_server_owned": lambda a, b: settle(b, "o:c", REJECTED_TIMEDOUT, NOW),
    "consistent_new_promise_born_clean": lambda a, b: (new_obj(b, "o:n", {"resonate:scope": "global"}), setattr(p(b, "o:n"), "callbacks", ["o:a"])),
    "consistent_task_acquisition_is_atomic": lambda a, b: (
        setattr(t(b, "o:b"), "state", T_ACQUIRED), setattr(t(b, "o:b"), "version", 1), setattr(t(b, "o:b"), "pid", "p"),
        setattr(t(b, "o:b"), "ttl", 10), setattr(t(b, "o:b"), "lease_at", NOW + 11), setattr(t(b, "o:b"), "retry_at", None)),
    "consistent_task_lease_deadline_is_now_plus_ttl": lambda a, b: setattr(t(b, "o:a"), "lease_at", 9_999),
    "consistent_task_pending_entry_arms_retry_fused": lambda a, b: (
        setattr(t(b, "o:s"), "state", T_PENDING), setattr(t(b, "o:s"), "retry_at", NOW), setattr(t(b, "o:s"), "resumes", {"o:e"}),
        setattr(p(b, "o:e"), "callbacks", []), b.outbox.append(Send(W, Execute("o:s", 1)))),
    "consistent_task_retry_rearm_only_when_due": lambda a, b: setattr(t(b, "o:b"), "retry_at", 40_000),
    "monotone_task_retry_rearm_advances": lambda a, b: (setattr(t(a, "o:b"), "retry_at", 50), setattr(t(b, "o:b"), "retry_at", 60)),
    "consistent_task_wake_records_resume_fused": lambda a, b: (
        setattr(t(b, "o:s"), "state", T_PENDING), setattr(t(b, "o:s"), "retry_at", NOW + 30_000), setattr(t(b, "o:s"), "resumes", set()),
        setattr(p(b, "o:e"), "callbacks", []), b.outbox.append(Send(W, Execute("o:s", 1)))),
}

#: Vacuous without schedules: nothing can change a schedule's birth fields.
VACUOUS = {"preserved_schedule_birth_fields_immutable"}

INTERNAL_VIOLATORS = {
    "consistent_task_state_edge_internal_admissible": lambda a, b: (
        setattr(t(b, "o:b"), "state", T_ACQUIRED), setattr(t(b, "o:b"), "version", 1), setattr(t(b, "o:b"), "pid", "p"),
        setattr(t(b, "o:b"), "ttl", 10), setattr(t(b, "o:b"), "lease_at", NOW + 10), setattr(t(b, "o:b"), "retry_at", None)),
    "consistent_promise_state_edge_internal_admissible": lambda a, b: settle(b, "o:c", RESOLVED, NOW),
}


def test_every_transition_property_has_a_violator():
    names = {n for n, _ in P.TRANS}
    assert names == set(TRANS_VIOLATORS) | VACUOUS, names ^ (set(TRANS_VIOLATORS) | VACUOUS)
    assert {n for n, _ in P.INTERNAL} == set(INTERNAL_VIOLATORS)


def test_each_transition_violator_is_rejected_by_its_own_entry():
    for name, violate in TRANS_VIOLATORS.items():
        a, b = base(), base()
        violate(a, b)
        assert name in P.trans_failures(NOW, a, b), name
    for name, violate in INTERNAL_VIOLATORS.items():
        a, b = base(), base()
        violate(a, b)
        assert name in P.internal_failures(NOW, a, b), name


def test_a_re_suspension_registers_nothing_new():
    """The path the specification's corpus never reaches: suspend, halt,
    continue, acquire, suspend on the same promise. The spec form of
    `consistent_suspension_registers_callback` rejects the last step; the
    adapted form, and the rest of the catalogue, accept it."""
    d = Document()
    d = apply(d, create("o:x", 1_000_000, {"resonate:scope": "global"}), 0)
    d = apply(d, create("o:a", 1_000_000, {"resonate:target": W}), 1)
    d = apply(d, TaskAcquire("o:a", 0, "p1", 5_000), 2)
    d = apply(d, TaskSuspend("o:a", 1, ("o:x",)), 3)
    d = apply(d, TaskHalt("o:a"), 4)
    d = apply(d, TaskContinue("o:a"), 5)
    d = apply(d, TaskAcquire("o:a", 1, "p1", 5_000), 6)
    a = P.State(d)
    d2, sends, reply = run(d, TaskSuspend("o:a", 2, ("o:x",)), 7)
    b = a.after(d2, sends)
    assert reply.status == 200 and d2.get("o:x").promise.callbacks == ["o:a"]
    assert P.trans_failures(7, a, b) == []
    assert not P.consistent_suspension_registers_callback(7, a, b)


def test_the_spec_forms_of_the_fused_entries_fail_on_our_fused_step():
    """Documenting the adaptation: the specification arms the retry at `now`
    and lets the retry step send; we fuse the two."""
    a = base()
    b, sends, _ = run(a.doc, PromiseSettle("o:e", RESOLVED), NOW)
    b = a.after(b, sends)
    assert P.trans_failures(NOW, a, b) == []
    assert not P.consistent_task_wake_records_resume(NOW, a, b)
    assert not P.consistent_task_pending_entry_arms_retry(NOW, a, b)
