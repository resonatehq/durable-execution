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
import random
from types import SimpleNamespace

import properties as P
from kernel import (
    PENDING, REJECTED, REJECTED_TIMEDOUT, RESOLVED, T_ACQUIRED, T_FULFILLED,
    T_HALTED, T_PENDING, T_SUSPENDED, Document, Execute, KernelCfg, Object,
    Promise, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, Send, SetDocument, Task,
    TaskAcquire, TaskContinue, TaskCreate, TaskFence, TaskFulfill, TaskGet,
    TaskHalt, TaskHeartbeat, TaskRelease, TaskSuspend, Unblock, Value,
    check_invariants, handle_external, handle_internal,
)

W = "http://w"
CFG = KernelCfg(retry_timeout=30_000)
P.RETRY_TIMEOUT = CFG.retry_timeout


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
    "consistent_suspension_registers_callback": lambda a, b: (
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


def test_the_spec_forms_of_the_fused_entries_fail_on_our_fused_step():
    """Documenting the adaptation: the specification arms the retry at `now`
    and lets the retry step send; we fuse the two."""
    a = base()
    b, sends, _ = run(a.doc, PromiseSettle("o:e", RESOLVED), NOW)
    b = a.after(b, sends)
    assert P.trans_failures(NOW, a, b) == []
    assert not P.consistent_task_wake_records_resume(NOW, a, b)
    assert not P.consistent_task_pending_entry_arms_retry(NOW, a, b)


# --- the walk --------------------------------------------------------------

IDS = ["o", "o:1", "o:2", "o:1.1", "o:2.1"]
FOREIGN = "x:1"
TAGS = [
    {},
    {"resonate:target": W},
    {"resonate:target": W, "resonate:branch": "o"},
    {"resonate:timer": "true"},
    {"resonate:scope": "global"},
    {"resonate:external": "true"},
    {"resonate:target": W, "resonate:delay": "DELAY"},
    {"resonate:timer": "true", "resonate:target": W},  # refused at the door
    {"resonate:target": ""},  # refused at the door
    {"resonate:target": "not a url"},  # refused at the door
]
TIMEOUTS = [1, 50, 500, 5_000, 100_000, 1_000_000]
LONG = [50_000, 1_000_000, 10_000_000]
STATES = [RESOLVED, REJECTED, "rejected_canceled", REJECTED_TIMEDOUT, "bogus"]


def gen_create(rng, now, id):
    tags = dict(rng.choice(TAGS))
    # Mostly a deadline that outlives the script, sometimes one already past.
    timeout = now + rng.choice(LONG) if rng.random() < 0.8 else rng.choice(TIMEOUTS)
    if tags.get("resonate:delay") == "DELAY":
        tags["resonate:delay"] = str(rng.choice([0, now, now + 10, now + 1_000, timeout, timeout + 1]))
    return create(id, timeout, tags)


def guided(rng, now, doc):
    """The next sensible move for this document, so the long chains (acquire,
    suspend, settle or expire, wake, acquire again, fulfil) are walked often
    enough for every guard to bite. Half the steps take it; the other half
    knock on every door with the raw alphabet."""
    tasks = [o for o in doc.objects if o.task is not None]
    acquired = [o for o in tasks if o.task.state == T_ACQUIRED]
    pending = [o for o in tasks if o.task.state == T_PENDING]
    suspended = [o for o in tasks if o.task.state == T_SUSPENDED]
    halted = [o for o in tasks if o.task.state == T_HALTED]
    awaitable = [o for o in doc.objects if o.promise.is_external() and o.promise.state == PENDING]
    settled = [o for o in doc.objects if o.promise.is_external() and o.promise.state != PENDING]
    moves = []
    for o in acquired:
        others = [x.id for x in awaitable if x.id != o.id]
        if others:
            moves.append(lambda o=o, others=others: TaskSuspend(o.id, o.task.version, tuple(rng.sample(others, rng.choice([1, min(2, len(others))])))))
        done = [x.id for x in settled if x.id != o.id]
        if done:
            # Suspending on something already settled: the 300 "carry on".
            moves.append(lambda o=o, done=done: TaskSuspend(o.id, o.task.version, (rng.choice(done),)))
        moves.append(lambda o=o: TaskFulfill(o.id, o.task.version, PromiseSettle(o.id, rng.choice([RESOLVED, REJECTED]), Value(data="v"))))
        moves.append(lambda o=o: TaskFence(o.id, o.task.version, "c", create(o.id + ".1" if ":" in o.id else o.id + ":1", now + 100_000, rng.choice([{"resonate:target": W}, {"resonate:scope": "global"}]))))
        moves.append(lambda o=o: TaskHeartbeat(o.task.pid, ((o.id, o.task.version),)))
    for o in pending:
        moves.append(lambda o=o: TaskAcquire(o.id, o.task.version, rng.choice(["p1", "p2"]), rng.choice([100, 5_000, 50_000])))
    for o in suspended:
        moves.append(lambda o=o: TaskHalt(o.id))  # a halted awaiter buffers the resume when its awaited settles
        for x in awaitable:
            if o.id in x.promise.callbacks:
                moves.append(lambda x=x: PromiseSettle(x.id, RESOLVED, Value(data="v")))
                moves.append(lambda o=o, x=x: PromiseRegisterCallback(x.id, o.id))
    for o in halted:
        for x in awaitable:
            if o.id in x.promise.callbacks:
                moves.append(lambda x=x: PromiseSettle(x.id, RESOLVED, Value(data="v")))
    for o in halted:
        moves.append(lambda o=o: TaskContinue(o.id))
    for x in awaitable:
        moves.append(lambda x=x: PromiseRegisterListener(x.id, rng.choice(["http://l1", "http://l2"])))
    if len(doc.objects) < 4:
        moves.append(lambda: create(rng.choice(IDS), now + rng.choice(LONG), rng.choice([{"resonate:target": W}, {"resonate:target": W}, {"resonate:scope": "global"}, {"resonate:timer": "true"}])))
    return rng.choice(moves)() if moves else None


def gen_request(rng, now, doc):
    """Adversarial, but steered: ids, versions, pids and awaited promises are
    drawn from the document most of the time, so the long chains (acquire,
    suspend, settle, wake) are actually reached, and from the raw alphabet
    the rest of the time, so every door is knocked on."""
    if rng.random() < 0.5:
        move = guided(rng, now, doc)
        if move is not None:
            return move
    existing = [o.id for o in doc.objects]
    pending_tasks = [o.id for o in doc.objects if o.task is not None and o.task.state == T_PENDING]
    acquired = [o.id for o in doc.objects if o.task is not None and o.task.state == T_ACQUIRED]
    pick = lambda pool: rng.choice(pool) if pool and rng.random() < 0.75 else (
        rng.choice(existing) if existing and rng.random() < 0.75 else rng.choice(IDS))
    id, other = pick(existing), rng.choice([pick(existing), FOREIGN, pick(existing)])
    acq, pen = pick(pending_tasks), pick(acquired)  # for acquire, and for the ops that need a holder
    o = doc.get(id)

    def ver(x):
        q = doc.get(x)
        return q.task.version if q is not None and q.task is not None and rng.random() < 0.85 else rng.choice([0, 1, 2, 3])

    def holder(x):
        q = doc.get(x)
        return q.task.pid if q is not None and q.task is not None and q.task.pid is not None and rng.random() < 0.85 else rng.choice(["p1", "p2"])

    version, pid = ver(id), holder(id)
    ttl = rng.choice([0, 1, 100, 5_000, 5_000, 50_000])
    awaitable = [x.id for x in doc.objects if x.promise.is_external() and x.promise.state == PENDING and x.id != pen]
    if awaitable and rng.random() < 0.85:
        awaited = tuple(rng.sample(awaitable, min(len(awaitable), rng.choice([1, 1, 2]))))
    else:
        awaited = tuple(rng.sample(IDS + [FOREIGN], rng.choice([0, 1, 2])) + ([pen] if rng.random() < 0.2 else []))
    settle_ = PromiseSettle(id, rng.choice(STATES + [RESOLVED, RESOLVED]), Value(data=rng.choice([None, "v"])))
    return rng.choice([
        lambda: PromiseGet(id),
        lambda: gen_create(rng, now, rng.choice(IDS)),
        lambda: gen_create(rng, now, rng.choice(IDS)),
        lambda: settle_,
        lambda: settle_,
        lambda: PromiseRegisterCallback(other, id),
        lambda: PromiseRegisterListener(id, rng.choice(["http://l1", "http://l2", "nope"])),
        lambda: TaskGet(id),
        lambda: TaskCreate(pid, ttl, gen_create(rng, now, rng.choice(IDS))),
        lambda: TaskAcquire(acq, ver(acq), pid, ttl),
        lambda: TaskAcquire(acq, ver(acq), pid, ttl),
        lambda: TaskRelease(pen, ver(pen)),
        lambda: TaskFulfill(pen, ver(pen), PromiseSettle(rng.choice([pen, pen, other]), settle_.state, settle_.value)),
        lambda: TaskSuspend(pen, ver(pen), awaited),
        lambda: TaskSuspend(pen, ver(pen), awaited),
        lambda: TaskFence(pen, ver(pen), "c", rng.choice([gen_create(rng, now, rng.choice(IDS)), PromiseSettle(other, settle_.state, settle_.value)])),
        lambda: TaskHeartbeat(holder(pen), ((pen, ver(pen)), (other, version))),
        lambda: TaskHalt(id),
        lambda: TaskContinue(id),
    ])()


def test_random_scripts_hold_the_whole_catalogue():
    rng = random.Random(20260921)
    seen = {"born_dead": 0, "wake": 0, "lease_expired": 0, "retry": 0, "carry_on_300": 0,
            "unblock": 0, "halted_buffer": 0, "born_acquired": 0, "delayed": 0, "door_400": 0}
    for _ in range(2_000):
        s, now = P.State(Document()), 0
        for _ in range(rng.randint(1, 16)):
            now += rng.choice([0, 0, 1, 10, 100, 1_000, 5_000, 5_000, 40_000] + ([2_000_000] if rng.random() < 0.05 else []))
            swept = handle_internal(s.doc, now, CFG)
            mid = s.after(next(e.doc for e in swept if isinstance(e, SetDocument)), [e for e in swept if isinstance(e, Send)])
            assert check_invariants(mid.doc) is None, check_invariants(mid.doc)
            assert P.state_failures(now, mid) == [], (P.state_failures(now, mid), mid.doc)
            assert P.trans_failures(now, s, mid) == [], (P.trans_failures(now, s, mid), s.doc, mid.doc)
            assert P.internal_failures(now, s, mid) == [], P.internal_failures(now, s, mid)
            if rng.random() < 0.15:
                nxt, reply, sends, doc = mid, None, [e for e in swept if isinstance(e, Send)], mid.doc
            else:
                req = gen_request(rng, now, s.doc)
                fx, reply = handle_external(mid.doc, req, now, CFG)
                doc = next(e.doc for e in fx if isinstance(e, SetDocument))
                sends = [e for e in fx if isinstance(e, Send)]
                nxt = mid.after(doc, sends)
                assert check_invariants(doc) is None, check_invariants(doc)
                assert P.state_failures(now, nxt) == [], (P.state_failures(now, nxt), doc)
                assert P.trans_failures(now, mid, nxt) == [], (P.trans_failures(now, mid, nxt), mid.doc, doc)
                fused, _ = handle_external(s.doc, req, now, CFG)
                assert next(e.doc for e in fused if isinstance(e, SetDocument)) == doc, "fused != composed"
            # Tally the transitions the guards need to have bitten.
            for o in doc.objects:
                a, b = s.doc.get(o.id), o
                if a is None and b.promise.state != PENDING:
                    seen["born_dead"] += 1
                if a is None and b.task is not None and b.task.state == T_ACQUIRED:
                    seen["born_acquired"] += 1
                if a is None and b.task is not None and b.task.state == T_PENDING and not sends:
                    seen["delayed"] += 1
                if a is not None and a.task is not None and b.task is not None:
                    if a.task.state == T_SUSPENDED and b.task.state == T_PENDING:
                        seen["wake"] += 1
                    if a.task.state == T_ACQUIRED and b.task.state == T_PENDING and reply is None:
                        seen["lease_expired"] += 1
                    if a.task.state == T_PENDING and b.task.state == T_PENDING and a.task.retry_at != b.task.retry_at:
                        seen["retry"] += 1
                    if b.task.state == T_HALTED and b.task.resumes - a.task.resumes:
                        seen["halted_buffer"] += 1
            if reply is not None and reply.status == 300:
                seen["carry_on_300"] += 1
            if reply is not None and reply.status == 400:
                seen["door_400"] += 1
            seen["unblock"] += sum(isinstance(e.msg, Unblock) for e in sends)
            s = nxt
    assert all(v > 0 for v in seen.values()), seen
