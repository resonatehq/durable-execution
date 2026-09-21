"""The conformance catalogue, transcribed from resonatehq/resonate-specification
(`spec/02-abstract/properties.lean`).

Every property is a predicate over a `State`: the document plus the outbox
the shell has accumulated from the kernel's `Send` effects. Two shapes:

    state(now, s)      true at every state the kernel passes through
    trans(now, a, b)   true at every pair of consecutive states, one step apart

`state_failures`, `trans_failures`, `internal_failures` and `gap_failures`
run a catalogue and return the NAMES of the properties that broke. The names
are the specification's own, so a violation means the same thing here as in
Lean, Go or TypeScript.

Three entries are adapted. Two are marked `_fused`: the abstract machine's
request steps only arm a task's retry deadline at `now`, and the internal
retry step then sends the execute message and re-arms at `now +
retry_timeout`; our kernel fuses those two steps into one, as the Rust kernel
does, so on entry to `pending` the deadline is already `now + retry_timeout`
and the execute is already in the outbox. One is marked `_adapted`:
`consistent_suspension_registers_callback` demands a callback NEW in the
step, but a task that suspends on a promise, is halted, continued,
re-acquired, and suspends on the same promise again registers nothing new,
because registration is idempotent in the specification's own `taskSuspend`.
The specification samples that entry on scripts of length three, which never
reach the five-step path; our walk did. The adapted form asks that the task
hold a registration on a pending promise after the step. The originals are
kept beside them as `SPEC_ONLY` for reference and are not walked.

Schedules are not implemented, so the six schedule properties hold over an
empty list. They are transcribed anyway so the catalogue is the whole
catalogue: 93 entries, 43 state and 50 transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from kernel import (
    PENDING, REJECTED, REJECTED_CANCELED, REJECTED_TIMEDOUT, RESOLVED,
    T_ACQUIRED, T_FULFILLED, T_HALTED, T_PENDING, T_SUSPENDED,
    TAG_TARGET, Document, Execute, Object, Promise, Send, Task, Unblock,
)

#: The server's dial, as the fused properties need it. Tests set it to the
#: KernelCfg they run with.
RETRY_TIMEOUT = 30_000


# ---------------------------------------------------------------------------
# The state the catalogue is stated over
# ---------------------------------------------------------------------------


def outbox_key(e: Send) -> tuple:
    """`OutboxEntry.key`: an execute is keyed by task id alone, so a
    redispatch replaces the row it repeats; an unblock by promise and address."""
    if isinstance(e.msg, Execute):
        return ("execute", e.msg.task_id)
    return ("notify", e.msg.promise["id"], e.address)


@dataclass
class State:
    doc: Document
    outbox: list[Send] = field(default_factory=list)  # upserted by key, never dropped
    schedules: tuple = ()  # not implemented; the schedule properties hold vacuously

    def after(self, doc: Document, sends: list[Send]) -> State:
        """The state one step later: the new document, and the outbox with
        this step's sends upserted by key."""
        entries = {outbox_key(e): e for e in self.outbox}
        for e in sends:
            entries[outbox_key(e)] = e
        return State(doc, list(entries.values()), self.schedules)

    # The two faces, as views of the one store.
    @property
    def promises(self) -> list[Promise]:
        return [o.promise for o in self.doc.objects]

    @property
    def tasks(self) -> list[Task]:
        return [o.task for o in self.doc.objects if o.task is not None]

    def promise(self, id: str) -> Promise | None:
        o = self.doc.get(id)
        return o.promise if o is not None else None

    def task(self, id: str) -> Task | None:
        o = self.doc.get(id)
        return o.task if o is not None else None

    def has(self, id: str) -> bool:
        return self.doc.get(id) is not None


def origin(id: str) -> str:
    return id.split(":", 1)[0]


def is_timer(p: Promise) -> bool:
    return p.tags.get("resonate:timer") == "true"


def timer_targeted(p: Promise) -> bool:
    return is_timer(p) and TAG_TARGET in p.tags


def runnable(p: Promise) -> bool:
    return TAG_TARGET in p.tags


def awaitable(p: Promise) -> bool:
    return p.is_external()


def value_empty(p: Promise) -> bool:
    return p.value.data is None and not p.value.headers


def project_state(p: Promise, now: int) -> str:
    """`PromiseObject.project`: a pending promise past its deadline reads as
    settled by that deadline."""
    if p.state == PENDING and p.timeout_at <= now:
        return RESOLVED if is_timer(p) else REJECTED_TIMEDOUT
    return p.state


def unique(xs) -> bool:
    xs = list(xs)
    return len(set(xs)) == len(xs)


def subset(xs, ys) -> bool:
    return all(x in ys for x in xs)


def cleared(t: Task) -> bool:
    return t.pid is None and t.ttl is None and t.lease_at is None and t.retry_at is None


# ---------------------------------------------------------------------------
# State properties
# ---------------------------------------------------------------------------


def well_formed_promise_created_at_lte_timeout_at(_now, s):
    return all(p.created_at <= p.timeout_at for p in s.promises)


def well_formed_promise_pending_created_before_deadline(_now, s):
    return all(p.state != PENDING or p.created_at < p.timeout_at for p in s.promises)


def well_formed_promise_settled_at_lte_timeout_at(_now, s):
    return all(p.settled_at is None or p.settled_at <= p.timeout_at for p in s.promises)


def well_formed_promise_created_at_lte_settled_at(_now, s):
    return all(p.settled_at is None or p.created_at <= p.settled_at for p in s.promises)


def well_formed_promise_settled_at_iff_not_pending(_now, s):
    return all((p.state != PENDING) == (p.settled_at is not None) for p in s.promises)


def well_formed_promise_pending_has_no_value(_now, s):
    return all(p.state != PENDING or value_empty(p) for p in s.promises)


def well_formed_promise_deadline_verdict_matches_timer_tag(_now, s):
    return all(
        p.settled_at != p.timeout_at or p.state == (RESOLVED if is_timer(p) else REJECTED_TIMEDOUT)
        for p in s.promises
    )


def well_formed_promise_deadline_settlement_has_no_value(_now, s):
    return all(p.settled_at != p.timeout_at or value_empty(p) for p in s.promises)


def well_formed_promise_timer_not_targeted(_now, s):
    return all(not timer_targeted(p) for p in s.promises)


def well_formed_promise_timedout_is_server_owned(_now, s):
    return all(p.state != REJECTED_TIMEDOUT or p.settled_at == p.timeout_at for p in s.promises)


def well_formed_promise_callbacks_unique(_now, s):
    return all(unique(p.callbacks) for p in s.promises)


def well_formed_promise_listeners_unique(_now, s):
    return all(unique(p.listeners) for p in s.promises)


def well_formed_promise_obligations_require_external(_now, s):
    return all((not p.callbacks and not p.listeners) or awaitable(p) for p in s.promises)


def well_formed_promise_awaiter_is_not_self(_now, s):
    return all(o.id not in o.promise.callbacks for o in s.doc.objects)


def well_formed_promise_callbacks_same_origin(_now, s):
    return all(all(origin(a) == origin(o.id) for a in o.promise.callbacks) for o in s.doc.objects)


def well_formed_promise_created_at_lte_now(now, s):
    return all(p.created_at <= now for p in s.promises)


def well_formed_promise_settled_at_lte_now(now, s):
    return all(p.settled_at is None or p.settled_at <= now for p in s.promises)


def well_formed_task_acquired_iff_has_pid(_now, s):
    return all((t.state == T_ACQUIRED) == (t.pid is not None) for t in s.tasks)


def well_formed_task_acquired_iff_has_ttl(_now, s):
    return all((t.state == T_ACQUIRED) == (t.ttl is not None) for t in s.tasks)


def well_formed_task_acquired_iff_has_lease_timeout_at(_now, s):
    return all((t.state == T_ACQUIRED) == (t.lease_at is not None) for t in s.tasks)


def well_formed_task_pending_iff_has_retry_timeout_at(_now, s):
    return all((t.state == T_PENDING) == (t.retry_at is not None) for t in s.tasks)


def well_formed_task_fulfilled_is_cleared(_now, s):
    return all(t.state != T_FULFILLED or (cleared(t) and not t.resumes) for t in s.tasks)


def well_formed_task_suspended_is_cleared(_now, s):
    return all(t.state != T_SUSPENDED or cleared(t) for t in s.tasks)


def well_formed_task_halted_is_cleared(_now, s):
    return all(t.state != T_HALTED or cleared(t) for t in s.tasks)


def well_formed_task_suspended_has_no_resumes(_now, s):
    return all(t.state != T_SUSPENDED or not t.resumes for t in s.tasks)


def well_formed_task_resumes_unique(_now, s):
    return True  # `resumes` is a set here; uniqueness is structural


def well_formed_task_acquired_version_positive(_now, s):
    return all(t.state != T_ACQUIRED or t.version >= 1 for t in s.tasks)


def well_formed_schedule_promise_tags_not_timer_targeted(_now, s):
    return all(not (c.promise_tags.get("resonate:timer") == "true" and TAG_TARGET in c.promise_tags) for c in s.schedules)


def well_formed_schedule_created_at_lte_next_run_at(_now, s):
    return all(c.created_at <= c.next_run_at for c in s.schedules)


def well_formed_schedule_created_at_lte_last_run_at(_now, s):
    return all(c.last_run_at is None or c.created_at <= c.last_run_at for c in s.schedules)


def well_formed_schedule_last_run_at_lt_next_run_at(_now, s):
    return all(c.last_run_at is None or c.last_run_at < c.next_run_at for c in s.schedules)


def well_formed_store_object_ids_unique(_now, s):
    return unique(o.id for o in s.doc.objects)


def well_formed_store_schedule_ids_unique(_now, s):
    return unique(c.id for c in s.schedules)


def well_formed_store_outbox_keys_unique(_now, s):
    return unique(outbox_key(e) for e in s.outbox)


def consistent_task_iff_kind_task(_now, s):
    return all((o.task is not None) == runnable(o.promise) for o in s.doc.objects)


def consistent_settled_promise_has_fulfilled_task(_now, s):
    return all(o.promise.state == PENDING or o.task is None or o.task.state == T_FULFILLED for o in s.doc.objects)


def consistent_callback_awaiter_is_targeted(_now, s):
    return all(
        all(any(q.id == a and runnable(q.promise) for q in s.doc.objects) for a in p.callbacks)
        for p in s.promises
    )


def consistent_outbox_execute_names_existing_task(_now, s):
    return all(not isinstance(e.msg, Execute) or s.task(e.msg.task_id) is not None for e in s.outbox)


def consistent_outbox_never_ahead(_now, s):
    for e in s.outbox:
        if isinstance(e.msg, Execute):
            t = s.task(e.msg.task_id)
            if t is not None and e.msg.version > t.version:
                return False
    return True


def consistent_outbox_execute_address_is_target_tag(_now, s):
    for e in s.outbox:
        if isinstance(e.msg, Execute):
            p = s.promise(e.msg.task_id)
            if p is not None and e.address != p.tags.get(TAG_TARGET, ""):
                return False
    return True


def consistent_outbox_unblock_names_settled_promise(_now, s):
    for e in s.outbox:
        if isinstance(e.msg, Unblock):
            r = e.msg.promise
            if r["state"] == PENDING:
                return False
            if not any(o.id == r["id"] and o.promise.state != PENDING for o in s.doc.objects):
                return False
    return True


def consistent_suspended_task_holds_rung(now, s):
    return all(
        o.task is None or o.task.state != T_SUSPENDED
        or project_state(o.promise, now) != PENDING
        or any(o.id in p.callbacks for p in s.promises)
        for o in s.doc.objects
    )


def consistent_settled_task_promise_settled(_now, s):
    return all(o.task is None or o.task.state != T_FULFILLED or o.promise.state != PENDING for o in s.doc.objects)


# ---------------------------------------------------------------------------
# Transition properties
# ---------------------------------------------------------------------------


def preserved_promise_birth_fields_immutable(_now, a, b):
    for o in a.doc.objects:
        p, q = o.promise, b.promise(o.id)
        if q is None:
            continue
        if not (q.param.data == p.param.data and q.param.headers == p.param.headers
                and q.tags == p.tags and q.timeout_at == p.timeout_at and q.created_at == p.created_at):
            return False
    return True


def preserved_settled_promise_record(_now, a, b):
    for o in a.doc.objects:
        p = o.promise
        if p.state == PENDING:
            continue
        q = b.promise(o.id)
        if q is None or not (q.state == p.state and q.settled_at == p.settled_at
                             and q.value.data == p.value.data and q.value.headers == p.value.headers):
            return False
    return True


def monotone_promise_set_grows(_now, a, b):
    return all(b.has(o.id) for o in a.doc.objects)


def monotone_task_set_grows(_now, a, b):
    return all(o.task is None or b.task(o.id) is not None for o in a.doc.objects)


def monotone_task_version_increases_only_on_acquisition(_now, a, b):
    """The fencing property: exactly +1 on pending -> acquired, unchanged otherwise."""
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_PENDING and u.state == T_ACQUIRED:
            if u.version != t.version + 1:
                return False
        elif u.version != t.version:
            return False
    return True


def preserved_fulfilled_task(_now, a, b):
    for o in a.doc.objects:
        t = o.task
        if t is None or t.state != T_FULFILLED:
            continue
        u = b.task(o.id)
        if u is None or not (u.state == T_FULFILLED and u.version == t.version and not u.resumes and cleared(u)):
            return False
    return True


def preserved_no_dead_dispatch(now, a, b):
    """No step puts a task into pending when its promise's deadline has passed."""
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_PENDING:
            continue
        t = a.task(o.id)
        if t is not None and t.state == T_PENDING:
            continue
        if project_state(o.promise, now) != PENDING:
            return False
    return True


def preserved_execute_only_for_live_task(now, a, b):
    for e in b.outbox:
        if not isinstance(e.msg, Execute):
            continue
        if any(isinstance(f.msg, Execute) and f.msg == e.msg and f.address == e.address for f in a.outbox):
            continue
        p = b.promise(e.msg.task_id)
        if p is not None and project_state(p, now) != PENDING:
            return False
    return True


def preserved_promise_state_frozen_once_settled(_now, a, b):
    for o in a.doc.objects:
        if o.promise.state == PENDING:
            continue
        q = b.promise(o.id)
        if q is None or q.state != o.promise.state:
            return False
    return True


def preserved_promise_settlement_is_one_way(_now, a, b):
    for o in b.doc.objects:
        if o.promise.state != PENDING:
            continue
        p = a.promise(o.id)
        if p is not None and p.state != PENDING:
            return False
    return True


def consistent_promise_settled_at_moves_with_state(_now, a, b):
    for o in a.doc.objects:
        q = b.promise(o.id)
        if q is None or (q.settled_at != o.promise.settled_at) != (q.state != o.promise.state):
            return False
    return True


def preserved_promise_value_until_settlement(_now, a, b):
    for o in a.doc.objects:
        p, q = o.promise, b.promise(o.id)
        if q is None:
            return False
        if q.state == PENDING and not (q.value.data == p.value.data and q.value.headers == p.value.headers):
            return False
    return True


def preserved_promise_no_duplicate_ids(_now, _a, b):
    return unique(o.id for o in b.doc.objects)


def monotone_promise_callbacks_grow_while_pending(_now, a, b):
    for o in b.doc.objects:
        q = o.promise
        if q.state != PENDING:
            continue
        p = a.promise(o.id)
        if (p is None and q.callbacks) or (p is not None and not subset(p.callbacks, q.callbacks)):
            return False
    return True


def monotone_promise_callbacks_shrink_once_settled(_now, a, b):
    for o in b.doc.objects:
        q = o.promise
        if q.state == PENDING:
            continue
        p = a.promise(o.id)
        if (p is None and q.callbacks) or (p is not None and not subset(q.callbacks, p.callbacks)):
            return False
    return True


def monotone_promise_listeners_grow_while_pending(_now, a, b):
    for o in b.doc.objects:
        q = o.promise
        if q.state != PENDING:
            continue
        p = a.promise(o.id)
        if (p is None and q.listeners) or (p is not None and not subset(p.listeners, q.listeners)):
            return False
    return True


def monotone_promise_listeners_shrink_once_settled(_now, a, b):
    for o in b.doc.objects:
        q = o.promise
        if q.state == PENDING:
            continue
        p = a.promise(o.id)
        if (p is None and q.listeners) or (p is not None and not subset(q.listeners, p.listeners)):
            return False
    return True


PROMISE_EDGES = {
    (PENDING, PENDING), (PENDING, RESOLVED), (PENDING, REJECTED),
    (PENDING, REJECTED_CANCELED), (PENDING, REJECTED_TIMEDOUT),
    (RESOLVED, RESOLVED), (REJECTED, REJECTED),
    (REJECTED_CANCELED, REJECTED_CANCELED), (REJECTED_TIMEDOUT, REJECTED_TIMEDOUT),
}

TASK_EDGES = {
    (T_PENDING, T_PENDING), (T_PENDING, T_ACQUIRED), (T_PENDING, T_HALTED), (T_PENDING, T_FULFILLED),
    (T_ACQUIRED, T_PENDING), (T_ACQUIRED, T_ACQUIRED), (T_ACQUIRED, T_SUSPENDED),
    (T_ACQUIRED, T_HALTED), (T_ACQUIRED, T_FULFILLED),
    (T_SUSPENDED, T_PENDING), (T_SUSPENDED, T_SUSPENDED), (T_SUSPENDED, T_HALTED), (T_SUSPENDED, T_FULFILLED),
    (T_HALTED, T_PENDING), (T_HALTED, T_HALTED), (T_HALTED, T_FULFILLED),
    (T_FULFILLED, T_FULFILLED),
}

TASK_EDGES_INTERNAL = {
    (T_PENDING, T_PENDING), (T_PENDING, T_FULFILLED),
    (T_ACQUIRED, T_PENDING), (T_ACQUIRED, T_ACQUIRED), (T_ACQUIRED, T_FULFILLED),
    (T_SUSPENDED, T_PENDING), (T_SUSPENDED, T_SUSPENDED), (T_SUSPENDED, T_FULFILLED),
    (T_HALTED, T_HALTED), (T_HALTED, T_FULFILLED),
    (T_FULFILLED, T_FULFILLED),
}


def consistent_promise_state_edge_admissible(_now, a, b):
    for o in a.doc.objects:
        q = b.promise(o.id)
        if q is not None and (o.promise.state, q.state) not in PROMISE_EDGES:
            return False
    return True


def consistent_task_state_edge_admissible(_now, a, b):
    for o in a.doc.objects:
        u = b.task(o.id)
        if o.task is not None and u is not None and (o.task.state, u.state) not in TASK_EDGES:
            return False
    return True


def preserved_task_acquisition_only_from_pending(_now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_ACQUIRED:
            continue
        t = a.task(o.id)
        if t is not None and t.state not in (T_PENDING, T_ACQUIRED):
            return False
    return True


def preserved_task_suspension_only_from_acquired(_now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_SUSPENDED:
            continue
        t = a.task(o.id)
        if t is None or t.state not in (T_ACQUIRED, T_SUSPENDED):
            return False
    return True


def preserved_task_halted_only_reenters_via_pending(_now, a, b):
    for o in a.doc.objects:
        t = o.task
        if t is None or t.state != T_HALTED:
            continue
        u = b.task(o.id)
        if u is None or u.state not in (T_HALTED, T_PENDING, T_FULFILLED):
            return False
    return True


def consistent_settlement_fulfils_task(_now, a, b):
    for o in a.doc.objects:
        if o.promise.state != PENDING:
            continue
        q, u = b.promise(o.id), b.task(o.id)
        if q is not None and u is not None and not (q.state == PENDING or u.state == T_FULFILLED):
            return False
    return True


def consistent_task_fulfilment_needs_settlement(_now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_FULFILLED:
            continue
        t = a.task(o.id)
        if t is None or t.state == T_FULFILLED:
            continue
        p, q = a.promise(o.id), b.promise(o.id)
        if not (p is not None and q is not None and p.state == PENDING and q.state != PENDING):
            return False
    return True


def consistent_obligation_discharge_requires_settled(_now, a, b):
    for o in a.doc.objects:
        p, q = o.promise, b.promise(o.id)
        if q is None:
            continue
        if not ((subset(p.callbacks, q.callbacks) and subset(p.listeners, q.listeners)) or q.state != PENDING):
            return False
    return True


def consistent_callback_consumption_resumes_awaiter(_now, a, b):
    for o in a.doc.objects:
        q = b.promise(o.id)
        for x in o.promise.callbacks:
            if q is not None and x in q.callbacks:
                continue
            u = b.task(x)
            if u is not None and not (u.state == T_FULFILLED or o.id in u.resumes):
                return False
    return True


def consistent_listener_consumption_enqueues_unblock(_now, a, b):
    for o in a.doc.objects:
        q = b.promise(o.id)
        for addr in o.promise.listeners:
            if q is not None and addr in q.listeners:
                continue
            n = sum(
                1 for e in b.outbox
                if e.address == addr and isinstance(e.msg, Unblock)
                and e.msg.promise["id"] == o.id and e.msg.promise["state"] != PENDING
            )
            if n != 1:
                return False
    return True


def consistent_wake_follows_callback_consumption(_now, a, b):
    for o in b.doc.objects:
        u, t = o.task, a.task(o.id)
        if u is None or t is None or not (t.state == T_SUSPENDED and u.state == T_PENDING):
            continue
        ok = any(
            o.id in p.promise.callbacks
            and b.promise(p.id) is not None and o.id not in b.promise(p.id).callbacks
            and p.id in u.resumes
            for p in a.doc.objects
        )
        if not ok:
            return False
    return True


def consistent_suspension_registers_callback(_now, a, b):
    """SPEC FORM: a task entering suspended registered a NEW callback. See `_adapted`."""
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_SUSPENDED:
            continue
        t = a.task(o.id)
        if t is not None and t.state == T_SUSPENDED:
            continue
        ok = any(
            o.id in q.promise.callbacks
            and (a.promise(q.id) is None or o.id not in a.promise(q.id).callbacks)
            and q.promise.state == PENDING
            for q in b.doc.objects
        )
        if not ok:
            return False
    return True


def consistent_suspension_registers_callback_adapted(_now, a, b):
    """A task entering suspended holds a registration on a pending promise
    after the step, whether this step made it or an earlier one did."""
    for o in b.doc.objects:
        u = o.task
        if u is None or u.state != T_SUSPENDED:
            continue
        t = a.task(o.id)
        if t is not None and t.state == T_SUSPENDED:
            continue
        if not any(o.id in q.promise.callbacks and q.promise.state == PENDING for q in b.doc.objects):
            return False
    return True


def consistent_task_birth_couples_promise_birth(_now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is not None and a.task(o.id) is None:
            q = o.promise
            born = (
                not a.has(o.id)
                and runnable(q)
                and (q.state != PENDING if u.state == T_FULFILLED else q.state == PENDING)
                and ((u.state == T_PENDING and u.version == 0)
                     or (u.state == T_ACQUIRED and u.version >= 1)
                     or (u.state == T_FULFILLED and u.version == 0))
            )
            if not born:
                return False
        if not a.has(o.id) and runnable(o.promise) and o.task is None:
            return False
    return True


def monotone_outbox_keys_never_disappear(_now, a, b):
    keys = {outbox_key(f) for f in b.outbox}
    return all(outbox_key(e) in keys for e in a.outbox)


def consistent_new_execute_matches_task_and_target(_now, a, b):
    for f in b.outbox:
        if not isinstance(f.msg, Execute):
            continue
        if any(isinstance(e.msg, Execute) and e.msg == f.msg and e.address == f.address for e in a.outbox):
            continue
        t, p = b.task(f.msg.task_id), b.promise(f.msg.task_id)
        if not (t is not None and t.version == f.msg.version and p is not None
                and f.address == p.tags.get(TAG_TARGET, "")):
            return False
    return True


def consistent_new_unblock_carries_stored_record(_now, a, b):
    for f in b.outbox:
        if not isinstance(f.msg, Unblock):
            continue
        r = f.msg.promise
        if any(isinstance(e.msg, Unblock) and e.address == f.address and e.msg.promise["id"] == r["id"] for e in a.outbox):
            continue
        p = b.promise(r["id"])
        if r["state"] == PENDING or p is None:
            return False
        if not (p.state == r["state"] and p.settled_at == r.get("settledAt")
                and p.value.data == r["value"].get("data") and p.timeout_at == r["timeoutAt"]
                and p.created_at == r["createdAt"]):
            return False
    return True


def consistent_new_unblock_discharges_its_listener(_now, a, b):
    for f in b.outbox:
        if not isinstance(f.msg, Unblock):
            continue
        r = f.msg.promise
        if any(isinstance(e.msg, Unblock) and e.address == f.address and e.msg.promise["id"] == r["id"] for e in a.outbox):
            continue
        had = any(o.id == r["id"] and f.address in o.promise.listeners for o in a.doc.objects)
        has = any(o.id == r["id"] and f.address in o.promise.listeners for o in b.doc.objects)
        if not (had and not has):
            return False
    return True


def preserved_schedule_birth_fields_immutable(_now, a, b):
    return True  # no schedules


def consistent_task_birth_state(_now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is None or a.task(o.id) is not None:
            continue
        ok = (
            (u.state == T_PENDING and u.retry_at is not None and u.pid is None and u.ttl is None
             and u.lease_at is None and not u.resumes)
            or (u.state == T_FULFILLED and cleared(u) and not u.resumes)
            or (u.state == T_ACQUIRED and u.version >= 1 and u.retry_at is None and u.pid is not None
                and u.ttl is not None and u.lease_at is not None and not u.resumes)
        )
        if not ok:
            return False
    return True


def consistent_task_lease_released_atomically(_now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_ACQUIRED and u.state != T_ACQUIRED:
            if not (u.pid is None and u.ttl is None and u.lease_at is None and u.version == t.version):
                return False
    return True


def preserved_task_lease_holder_stable(_now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_ACQUIRED and u.state == T_ACQUIRED and u.version == t.version:
            if not (u.pid == t.pid and u.ttl == t.ttl):
                return False
    return True


def consistent_task_lease_fields_move_together(_now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        ok = (
            (u.pid == t.pid and u.ttl == t.ttl and u.lease_at == t.lease_at)
            or (t.state != T_ACQUIRED and u.state == T_ACQUIRED
                and u.pid is not None and u.ttl is not None and u.lease_at is not None)
            or (t.state == T_ACQUIRED and u.state != T_ACQUIRED
                and u.pid is None and u.ttl is None and u.lease_at is None)
            or (t.state == T_ACQUIRED and u.state == T_ACQUIRED and u.pid == t.pid and u.ttl == t.ttl)
        )
        if not ok:
            return False
    return True


def monotone_task_resumes_grow_or_clear(_now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if u.resumes and not subset(t.resumes, u.resumes):
            return False
    return True


def consistent_task_resumes_cleared_only_on_dispatch_or_park(_now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.resumes and not u.resumes and u.state not in (T_ACQUIRED, T_SUSPENDED, T_FULFILLED):
            return False
    return True


def consistent_task_acquisition_is_atomic(now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state != T_ACQUIRED and u.state == T_ACQUIRED:
            if not (t.state == T_PENDING and t.version < u.version and u.pid is not None
                    and u.ttl is not None and u.lease_at == now + u.ttl
                    and u.retry_at is None and not u.resumes):
                return False
    return True


def consistent_task_lease_deadline_is_now_plus_ttl(now, a, b):
    for o in b.doc.objects:
        u = o.task
        if u is None or u.lease_at is None:
            continue
        if u.lease_at == now + (u.ttl or 0):
            continue
        t = a.task(o.id)
        if not (t is not None and t.lease_at == u.lease_at and t.ttl == u.ttl and t.state == u.state):
            return False
    return True


def consistent_task_pending_entry_arms_retry(now, a, b):
    """SPEC FORM: entering pending arms the retry at `now`. See `_fused`."""
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state != T_PENDING and u.state == T_PENDING and u.retry_at != now:
            return False
    return True


def consistent_task_pending_entry_arms_retry_fused(now, a, b):
    """Fused: entering pending arms the retry at `now + retry_timeout` and the
    execute message for the task's version is in the outbox."""
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state != T_PENDING and u.state == T_PENDING:
            sent = any(isinstance(e.msg, Execute) and e.msg == Execute(o.id, u.version) for e in b.outbox)
            if u.retry_at != now + RETRY_TIMEOUT or not sent:
                return False
    return True


def consistent_task_retry_rearm_only_when_due(now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_PENDING and u.state == T_PENDING and u.retry_at != t.retry_at:
            if t.retry_at is None or t.retry_at > now:
                return False
    return True


def monotone_task_retry_rearm_advances(now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None or t.state != T_PENDING or u.state != T_PENDING:
            continue
        if u.retry_at != t.retry_at and not (u.retry_at is not None and now < u.retry_at):
            return False
    return True


def consistent_task_wake_records_resume(now, a, b):
    """SPEC FORM: a wake arms the retry at `now`. See `_fused`."""
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_SUSPENDED and u.state == T_PENDING:
            if not (u.resumes and u.retry_at == now and u.version == t.version):
                return False
    return True


def consistent_task_wake_records_resume_fused(now, a, b):
    for o in a.doc.objects:
        t, u = o.task, b.task(o.id)
        if t is None or u is None:
            continue
        if t.state == T_SUSPENDED and u.state == T_PENDING:
            sent = any(isinstance(e.msg, Execute) and e.msg == Execute(o.id, u.version) for e in b.outbox)
            if not (u.resumes and u.retry_at == now + RETRY_TIMEOUT and u.version == t.version and sent):
                return False
    return True


def consistent_promise_settlement_stamp(now, a, b):
    """The settlement dichotomy: a client verdict at `now`, strictly before the
    deadline, never timed out; or the deadline, stamped at the deadline,
    verdict by the timer tag, value untouched."""
    for o in a.doc.objects:
        p = o.promise
        if p.state != PENDING:
            continue
        q = b.promise(o.id)
        if q is None:
            return False
        if q.state == PENDING:
            continue
        by_client = q.settled_at == now and now < q.timeout_at and q.state != REJECTED_TIMEDOUT
        by_deadline = (
            q.settled_at == q.timeout_at and q.timeout_at <= now
            and q.state == (RESOLVED if is_timer(q) else REJECTED_TIMEDOUT)
            and q.value.data == p.value.data and q.value.headers == p.value.headers
        )
        if not (by_client or by_deadline):
            return False
    return True


def preserved_timedout_is_server_owned(now, a, b):
    for o in a.doc.objects:
        p = o.promise
        if p.state != PENDING:
            continue
        q = b.promise(o.id)
        if q is not None and q.state == REJECTED_TIMEDOUT and not (p.timeout_at <= now and q.settled_at == p.timeout_at):
            return False
    return True


def consistent_new_promise_born_clean(now, a, b):
    for o in b.doc.objects:
        if a.has(o.id):
            continue
        q = o.promise
        clean = (
            not q.callbacks and not q.listeners and value_empty(q) and q.created_at <= now
            and ((q.state == PENDING and q.settled_at is None and q.created_at < q.timeout_at)
                 or (q.settled_at == q.timeout_at and q.created_at == q.timeout_at and q.timeout_at <= now
                     and q.state == (RESOLVED if is_timer(q) else REJECTED_TIMEDOUT)))
        )
        if not clean:
            return False
    return True


# ---------------------------------------------------------------------------
# The sweeper properties: internal steps only
# ---------------------------------------------------------------------------


def consistent_task_state_edge_internal_admissible(_now, a, b):
    for o in a.doc.objects:
        u = b.task(o.id)
        if o.task is not None and u is not None and (o.task.state, u.state) not in TASK_EDGES_INTERNAL:
            return False
    return True


def consistent_promise_state_edge_internal_admissible(_now, a, b):
    for o in a.doc.objects:
        p, q = o.promise, b.promise(o.id)
        if q is None or p.state == q.state:
            continue
        if not (p.state == PENDING and (q.state == REJECTED_TIMEDOUT or (q.state == RESOLVED and is_timer(p)))):
            return False
    return True


# ---------------------------------------------------------------------------
# Known gaps: true of the protocol, enforced at our doors
# ---------------------------------------------------------------------------


def well_formed_task_ttl_positive(_now, s):
    return all(t.state != T_ACQUIRED or (t.ttl or 0) > 0 for t in s.tasks)


def well_formed_promise_target_is_nonempty(_now, s):
    return all(TAG_TARGET not in p.tags or p.tags[TAG_TARGET] != "" for p in s.promises)


def well_formed_promise_delay_before_deadline(_now, s):
    for p in s.promises:
        d = p.tags.get("resonate:delay")
        if d is not None and not (d.isdigit() and int(d) < p.timeout_at):
            return False
    return True


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

STATE: list[tuple[str, Callable[[int, State], bool]]] = [
    (f.__name__, f) for f in [
        well_formed_promise_created_at_lte_timeout_at,
        well_formed_promise_pending_created_before_deadline,
        well_formed_promise_settled_at_lte_timeout_at,
        well_formed_promise_created_at_lte_settled_at,
        well_formed_promise_settled_at_iff_not_pending,
        well_formed_promise_pending_has_no_value,
        well_formed_promise_deadline_verdict_matches_timer_tag,
        well_formed_promise_deadline_settlement_has_no_value,
        well_formed_promise_timer_not_targeted,
        well_formed_promise_timedout_is_server_owned,
        well_formed_promise_callbacks_unique,
        well_formed_promise_listeners_unique,
        well_formed_promise_obligations_require_external,
        well_formed_promise_awaiter_is_not_self,
        well_formed_promise_callbacks_same_origin,
        well_formed_promise_created_at_lte_now,
        well_formed_promise_settled_at_lte_now,
        well_formed_task_acquired_iff_has_pid,
        well_formed_task_acquired_iff_has_ttl,
        well_formed_task_acquired_iff_has_lease_timeout_at,
        well_formed_task_pending_iff_has_retry_timeout_at,
        well_formed_task_fulfilled_is_cleared,
        well_formed_task_suspended_is_cleared,
        well_formed_task_halted_is_cleared,
        well_formed_task_suspended_has_no_resumes,
        well_formed_task_resumes_unique,
        well_formed_task_acquired_version_positive,
        well_formed_schedule_promise_tags_not_timer_targeted,
        well_formed_schedule_created_at_lte_next_run_at,
        well_formed_schedule_created_at_lte_last_run_at,
        well_formed_schedule_last_run_at_lt_next_run_at,
        well_formed_store_object_ids_unique,
        well_formed_store_schedule_ids_unique,
        well_formed_store_outbox_keys_unique,
        consistent_task_iff_kind_task,
        consistent_settled_promise_has_fulfilled_task,
        consistent_callback_awaiter_is_targeted,
        consistent_outbox_execute_names_existing_task,
        consistent_outbox_never_ahead,
        consistent_outbox_execute_address_is_target_tag,
        consistent_outbox_unblock_names_settled_promise,
        consistent_settled_task_promise_settled,
        consistent_suspended_task_holds_rung,
    ]
]

TRANS: list[tuple[str, Callable[[int, State, State], bool]]] = [
    (f.__name__, f) for f in [
        preserved_promise_birth_fields_immutable,
        preserved_settled_promise_record,
        monotone_promise_set_grows,
        monotone_task_set_grows,
        monotone_task_version_increases_only_on_acquisition,
        preserved_fulfilled_task,
        preserved_promise_state_frozen_once_settled,
        preserved_promise_settlement_is_one_way,
        consistent_promise_settled_at_moves_with_state,
        preserved_promise_value_until_settlement,
        preserved_promise_no_duplicate_ids,
        monotone_promise_callbacks_grow_while_pending,
        monotone_promise_callbacks_shrink_once_settled,
        monotone_promise_listeners_grow_while_pending,
        monotone_promise_listeners_shrink_once_settled,
        consistent_promise_state_edge_admissible,
        consistent_task_state_edge_admissible,
        preserved_task_acquisition_only_from_pending,
        preserved_task_suspension_only_from_acquired,
        preserved_task_halted_only_reenters_via_pending,
        consistent_settlement_fulfils_task,
        consistent_task_fulfilment_needs_settlement,
        consistent_obligation_discharge_requires_settled,
        consistent_callback_consumption_resumes_awaiter,
        consistent_listener_consumption_enqueues_unblock,
        consistent_wake_follows_callback_consumption,
        consistent_suspension_registers_callback_adapted,
        consistent_task_birth_couples_promise_birth,
        monotone_outbox_keys_never_disappear,
        consistent_new_execute_matches_task_and_target,
        consistent_new_unblock_carries_stored_record,
        consistent_new_unblock_discharges_its_listener,
        preserved_schedule_birth_fields_immutable,
        consistent_task_birth_state,
        consistent_task_lease_released_atomically,
        preserved_task_lease_holder_stable,
        consistent_task_lease_fields_move_together,
        monotone_task_resumes_grow_or_clear,
        consistent_task_resumes_cleared_only_on_dispatch_or_park,
        preserved_no_dead_dispatch,
        preserved_execute_only_for_live_task,
        consistent_promise_settlement_stamp,
        preserved_timedout_is_server_owned,
        consistent_new_promise_born_clean,
        consistent_task_acquisition_is_atomic,
        consistent_task_lease_deadline_is_now_plus_ttl,
        consistent_task_pending_entry_arms_retry_fused,
        consistent_task_retry_rearm_only_when_due,
        monotone_task_retry_rearm_advances,
        consistent_task_wake_records_resume_fused,
    ]
]

#: The entries whose specification form is not walked: two assume unfused
#: steps, one a corpus too short to reach a re-suspension.
SPEC_ONLY = [
    consistent_task_pending_entry_arms_retry,
    consistent_task_wake_records_resume,
    consistent_suspension_registers_callback,
]

INTERNAL = [
    (f.__name__, f) for f in [
        consistent_task_state_edge_internal_admissible,
        consistent_promise_state_edge_internal_admissible,
    ]
]

GAPS = [
    (f.__name__, f) for f in [
        well_formed_task_ttl_positive,
        well_formed_promise_target_is_nonempty,
        well_formed_promise_delay_before_deadline,
    ]
]


def state_failures(now: int, s: State) -> list[str]:
    return [name for name, f in STATE + GAPS if not f(now, s)]


def trans_failures(now: int, a: State, b: State) -> list[str]:
    return [name for name, f in TRANS if not f(now, a, b)]


def internal_failures(now: int, a: State, b: State) -> list[str]:
    return [name for name, f in INTERNAL if not f(now, a, b)]
