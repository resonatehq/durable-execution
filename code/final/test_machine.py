"""The kernel as a Hypothesis state machine.

This replaces a hand-rolled random walk. What the port buys, and the reason
for the dependency: when a script violates the catalogue, Hypothesis shrinks
it to its minimal form and stores it, so the next run replays it first. The
walk it replaces found one real defect and handed over two document dumps to
read; this hands over the shortest script that breaks.

The steering the walk did by hand is `@precondition`: a rule that needs an
acquired task only fires when the document holds one, so the long chains
(acquire, suspend, settle, wake, re-acquire, fulfil) are walked rather than
stumbled into. Ids, tags and deadlines still come from an adversarial
alphabet, so the doors are knocked on too — but only where no guided twin
exists. Breadth over refusals is `explore.py`'s job, which enumerates them;
this machine is for the long chains, which no exhaustive search reaches.

Each rule runs one request through both abstract halves, as the other suites
do: the sweep as an internal step and the operation as an external step, with
the whole catalogue on each, and the fused result held equal to the
composition. The state properties are `@invariant`s, so a break is reported
as one.

Exhaustive coverage is `explore.py`'s job, not this file's, and the split is
deliberate: a random search is the wrong instrument for proving a state is
reachable, and an exhaustive one is the wrong instrument for scripts longer
than its bound. What is asserted here is only that the campaign was not
vacuous — that it built and drove real tasks rather than spending itself on
refusals. The deep chains are `test_explore.py`'s narrow profile.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine, invariant, precondition, rule, run_state_machine_as_test,
)

import properties as P
from kernel import (
    PENDING, REJECTED, REJECTED_CANCELED, RESOLVED, T_ACQUIRED, T_FULFILLED,
    T_HALTED, T_PENDING, T_SUSPENDED, Document, Execute, KernelCfg,
    PromiseCreate, PromiseGet, PromiseRegisterCallback, PromiseRegisterListener,
    PromiseSettle, Send, SetDocument, TaskAcquire, TaskContinue, TaskCreate,
    TaskFence, TaskFulfill, TaskGet, TaskHalt, TaskHeartbeat, TaskRelease,
    TaskSuspend, Unblock, Value, check_invariants, handle_external, handle_internal,
)

W = "http://w"
CFG = KernelCfg(retry_timeout=30_000)

#: Six, not four. Creation is first-writer-wins, so an id that takes an
#: adversarial promise early is spent for the rest of the script; with too few,
#: a campaign runs out of ids before it can build a chain.
IDS = ["o", "o:1", "o:2", "o:3", "o:1.1", "o:2.1"]
FOREIGN = "x:1"  # another origin: every door that compares origins must refuse it
TAGS = [
    {},
    {"resonate:target": W},
    {"resonate:target": W, "resonate:branch": "o"},
    {"resonate:timer": "true"},
    {"resonate:scope": "global"},
    {"resonate:external": "true"},
    {"resonate:target": W, "resonate:delay": "DELAY"},
    {"resonate:timer": "true", "resonate:target": W},  # refused at the door
    {"resonate:target": ""},                           # refused at the door
    {"resonate:target": "not a url"},                  # refused at the door
]

ids = st.sampled_from(IDS)
any_ids = st.sampled_from(IDS + [FOREIGN])
tags = st.sampled_from(TAGS)
timeouts = st.sampled_from([1, 500, 100_000, 10_000_000])
#: Deadlines are mostly relative and generous, so a script has room to build a
#: chain before its promises expire under it. Absolute ones are the adversarial
#: case: a deadline already in the past, born dead.
absolute = st.sampled_from([False, False, False, False, True])
delays = st.sampled_from([0, 10, 1_000, 100_000])
versions = st.integers(min_value=0, max_value=3)
ttls = st.sampled_from([0, 1, 5_000, 50_000])
pids = st.sampled_from(["p1", "p2"])
addresses = st.sampled_from(["http://l1", "http://l2", "nope"])
settle_states = st.sampled_from([RESOLVED, REJECTED, REJECTED_CANCELED, "rejected_timedout", "bogus"])
payloads = st.sampled_from([Value(), Value(data="v")])
#: An index into a state-dependent pool, taken modulo its length. An integer
#: rather than a `data.draw`, because it shrinks to 0 and keeps the script
#: readable.
idx = st.integers(min_value=0, max_value=7)

#: What the campaign reached, accumulated across every example. Asserted once,
#: after the run, so a campaign that only ever knocked on doors fails.
SEEN: Counter = Counter()


class KernelMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.now = 0
        self.s = P.State(Document(), retry_timeout=CFG.retry_timeout)

    # ── the pools the preconditions read ──────────────────────────────────

    def _with_task(self, *states):
        return [o for o in self.s.doc.objects if o.task is not None and o.task.state in states]

    def _awaitable(self, pending=True):
        return [o for o in self.s.doc.objects
                if o.promise.is_external() and (o.promise.state == PENDING) == pending]

    @staticmethod
    def _pick(pool, i):
        return pool[i % len(pool)]

    # ── one step, checked ─────────────────────────────────────────────────

    def _sweep(self):
        """The internal half, which every request runs first."""
        fx = handle_internal(self.s.doc, self.now, CFG)
        doc = next(e.doc for e in fx if isinstance(e, SetDocument))
        sends = [e for e in fx if isinstance(e, Send)]
        mid = self.s.after(doc, sends)
        assert check_invariants(doc) is None, check_invariants(doc)
        assert P.state_failures(self.now, mid) == [], P.state_failures(self.now, mid)
        assert P.trans_failures(self.now, self.s, mid) == [], P.trans_failures(self.now, self.s, mid)
        assert P.internal_failures(self.now, self.s, mid) == [], P.internal_failures(self.now, self.s, mid)
        self._tally(self.s, mid, sends, None)
        return mid

    def do(self, req):
        """Sweep, then the request against the swept document, each checked as
        its own abstract step, and the fused result held equal to both."""
        before = self.s
        mid = self._sweep()
        fx, reply = handle_external(mid.doc, req, self.now, CFG)
        doc = next(e.doc for e in fx if isinstance(e, SetDocument))
        sends = [e for e in fx if isinstance(e, Send)]
        after = mid.after(doc, sends)
        assert check_invariants(doc) is None, check_invariants(doc)
        assert P.state_failures(self.now, after) == [], P.state_failures(self.now, after)
        assert P.trans_failures(self.now, mid, after) == [], P.trans_failures(self.now, mid, after)
        fused, _ = handle_external(before.doc, req, self.now, CFG)
        assert next(e.doc for e in fused if isinstance(e, SetDocument)) == doc, "fused != composed"
        self._tally(mid, after, sends, reply)
        event(f"{type(req).__name__} {reply.status}")
        self.s = after
        return reply

    def _tally(self, a, b, sends, reply):
        for o in b.doc.objects:
            x = a.doc.get(o.id)
            if x is None:
                SEEN["born_dead" if o.promise.state != PENDING else "born_pending"] += 1
                if o.task is not None and o.task.state == T_ACQUIRED:
                    SEEN["born_acquired"] += 1
                if o.task is not None and o.task.state == T_PENDING and not sends:
                    SEEN["delayed"] += 1
                continue
            if x.promise.state == PENDING and o.promise.state != PENDING:
                SEEN["expired" if o.promise.settled_at == o.promise.timeout_at else "settled"] += 1
            if x.task is None or o.task is None:
                continue
            if x.task.state == T_SUSPENDED and o.task.state == T_PENDING:
                SEEN["wake"] += 1
            if x.task.state == T_ACQUIRED and o.task.state == T_PENDING and reply is None:
                SEEN["lease_expired"] += 1
            if x.task.state == T_PENDING and o.task.state == T_PENDING and x.task.retry_at != o.task.retry_at:
                SEEN["retry"] += 1
            if o.task.state == T_HALTED and o.task.resumes - x.task.resumes:
                SEEN["halted_buffer"] += 1
            if o.task.state == T_ACQUIRED and o.task.resumes - x.task.resumes:
                SEEN["resumed_while_running"] += 1
            if x.task.state == T_ACQUIRED and o.task.state == T_SUSPENDED:
                SEEN["suspended"] += 1
            if x.task.state == T_PENDING and o.task.state == T_ACQUIRED:
                SEEN["acquired"] += 1
        if reply is not None:
            SEEN["carry_on_300" if reply.status == 300 else
                 "refused" if reply.status >= 400 else "ok"] += 1
        SEEN["unblock"] += sum(isinstance(e.msg, Unblock) for e in sends)
        SEEN["execute"] += sum(isinstance(e.msg, Execute) for e in sends)

    # ── the clock, and the internal message ───────────────────────────────

    @rule(by=st.sampled_from([1, 1, 100, 100, 5_000, 30_000, 200_000]))
    def advance(self, by):
        self.now += by

    @rule()
    def timeout(self):
        """What a timer fires: the sweep alone, with no request behind it."""
        self.s = self._sweep()

    # ── promises ──────────────────────────────────────────────────────────

    @rule(id=ids, to=timeouts, tag=tags, delay=delays, absolute=absolute)
    def promise_create(self, id, to, tag, delay, absolute):
        tag = dict(tag)
        if tag.get("resonate:delay") == "DELAY":
            tag["resonate:delay"] = str(delay if absolute else self.now + delay)
        self.do(PromiseCreate(id, to if absolute else self.now + to, Value(), tag))

    @rule(id=ids, kind=st.sampled_from(["target", "scope", "timer"]))
    def create_something_long_lived(self, id, kind):
        """A promise that outlives the script. Without these the alphabet
        expires everything it builds and the long chains are never walked."""
        tag = {"target": {"resonate:target": W}, "scope": {"resonate:scope": "global"},
               "timer": {"resonate:timer": "true"}}[kind]
        self.do(PromiseCreate(id, self.now + 10_000_000, Value(), tag))

    @rule(id=ids)
    def promise_get(self, id):
        self.do(PromiseGet(id))

    @precondition(lambda self: self._awaitable())
    @rule(i=idx, state=settle_states, value=payloads)
    def settle_something_awaited(self, i, state, value):
        """The settle that fans out: it wakes suspended awaiters and unblocks
        listeners, which the blind rule above reaches only by luck."""
        self.do(PromiseSettle(self._pick(self._awaitable(), i).id, state, value))

    @precondition(lambda self: self._awaitable() and self._with_task(T_PENDING, T_ACQUIRED, T_SUSPENDED, T_HALTED))
    @rule(i=idx, j=idx)
    def register_a_real_callback(self, i, j):
        awaited = self._pick(self._awaitable(), i)
        awaiter = self._pick(self._with_task(T_PENDING, T_ACQUIRED, T_SUSPENDED, T_HALTED), j)
        self.do(PromiseRegisterCallback(awaited.id, awaiter.id))

    @precondition(lambda self: self._awaitable(pending=False) and self._with_task(T_SUSPENDED, T_ACQUIRED))
    @rule(i=idx, j=idx)
    def register_against_a_settled_promise(self, i, j):
        """The branch that resumes instead of registering."""
        awaited = self._pick(self._awaitable(pending=False), i)
        awaiter = self._pick(self._with_task(T_SUSPENDED, T_ACQUIRED), j)
        self.do(PromiseRegisterCallback(awaited.id, awaiter.id))

    @precondition(lambda self: self._awaitable())
    @rule(i=idx, address=addresses)
    def listen_to_something_awaitable(self, i, address):
        self.do(PromiseRegisterListener(self._pick(self._awaitable(), i).id, address))

    # ── tasks ─────────────────────────────────────────────────────────────

    @rule(id=ids)
    def task_get(self, id):
        self.do(TaskGet(id))

    @rule(id=ids, pid=pids, ttl=st.sampled_from([0, 1, 5_000, 50_000, 10_000_000]), to=timeouts, tag=tags)
    def task_create(self, id, pid, ttl, to, tag):
        self.do(TaskCreate(pid, ttl, PromiseCreate(id, self.now + to, Value(), dict(tag))))

    @precondition(lambda self: self._with_task(T_PENDING))
    @rule(i=idx, pid=pids, ttl=st.sampled_from([1, 5_000, 50_000, 10_000_000]))
    def acquire_a_pending_task(self, i, pid, ttl):
        o = self._pick(self._with_task(T_PENDING), i)
        self.do(TaskAcquire(o.id, o.task.version, pid, ttl))

    @precondition(lambda self: self._with_task(T_ACQUIRED))
    @rule(i=idx)
    def release_an_acquired_task(self, i):
        o = self._pick(self._with_task(T_ACQUIRED), i)
        self.do(TaskRelease(o.id, o.task.version))

    @precondition(lambda self: self._with_task(T_ACQUIRED))
    @rule(i=idx, state=st.sampled_from([RESOLVED, REJECTED]), value=payloads)
    def fulfil_an_acquired_task(self, i, state, value):
        o = self._pick(self._with_task(T_ACQUIRED), i)
        self.do(TaskFulfill(o.id, o.task.version, PromiseSettle(o.id, state, value)))

    @precondition(lambda self: self._with_task(T_ACQUIRED) and len(self._awaitable()) > 1)
    @rule(i=idx, j=idx, two=st.booleans())
    def suspend_an_acquired_task(self, i, j, two):
        o = self._pick(self._with_task(T_ACQUIRED), i)
        pool = [x.id for x in self._awaitable() if x.id != o.id]
        if not pool:
            return
        awaited = (self._pick(pool, j),)
        if two and len(pool) > 1:
            awaited += (self._pick(pool, j + 1),)
        self.do(TaskSuspend(o.id, o.task.version, tuple(dict.fromkeys(awaited))))

    @precondition(lambda self: self._with_task(T_ACQUIRED) and self._awaitable(pending=False))
    @rule(i=idx, j=idx)
    def suspend_on_something_settled(self, i, j):
        """Nothing to wait for: the 300 that tells the caller to carry on."""
        o = self._pick(self._with_task(T_ACQUIRED), i)
        pool = [x.id for x in self._awaitable(pending=False) if x.id != o.id]
        if pool:
            self.do(TaskSuspend(o.id, o.task.version, (self._pick(pool, j),)))

    @rule(id=ids, version=versions, awaited=st.lists(any_ids, max_size=3).map(tuple))
    def a_request_that_may_be_refused(self, id, version, awaited):
        """The doors no guided rule reaches: a wrong version, a foreign
        origin, an empty or duplicated awaited list, an id with no task.
        One rule rather than a blind twin per operation, because a campaign
        spent on refusals never builds a chain."""
        self.do(TaskSuspend(id, version, awaited))

    @precondition(lambda self: self._with_task(T_ACQUIRED))
    @rule(i=idx, other=ids, to=timeouts, tag=tags, settle=st.booleans(), state=settle_states)
    def fence_an_action(self, i, other, to, tag, settle, state):
        o = self._pick(self._with_task(T_ACQUIRED), i)
        action = (PromiseSettle(other, state, Value())
                  if settle else PromiseCreate(other, self.now + to, Value(), dict(tag)))
        self.do(TaskFence(o.id, o.task.version, "c", action))

    @precondition(lambda self: self._with_task(T_ACQUIRED))
    @rule(i=idx)
    def heartbeat_a_held_task(self, i):
        o = self._pick(self._with_task(T_ACQUIRED), i)
        self.do(TaskHeartbeat(o.task.pid, ((o.id, o.task.version),)))

    @precondition(lambda self: self._with_task(T_PENDING, T_ACQUIRED, T_SUSPENDED))
    @rule(i=idx)
    def halt_a_live_task(self, i):
        """A halted awaiter buffers a resume instead of being dispatched."""
        self.do(TaskHalt(self._pick(self._with_task(T_PENDING, T_ACQUIRED, T_SUSPENDED), i).id))

    @precondition(lambda self: self._with_task(T_HALTED))
    @rule(i=idx)
    def continue_a_halted_task(self, i):
        self.do(TaskContinue(self._pick(self._with_task(T_HALTED), i).id))

    def _awaited_by(self, *states):
        """Pending awaitable promises with an awaiter whose task is in one of
        `states`. What a settlement has to fan out to."""
        who = {o.id for o in self._with_task(*states)}
        return [o for o in self._awaitable() if who & set(o.promise.callbacks)]

    @precondition(lambda self: self._awaited_by(T_HALTED, T_ACQUIRED, T_PENDING))
    @rule(i=idx, state=st.sampled_from([RESOLVED, REJECTED]),
          who=st.sampled_from([(T_HALTED,), (T_ACQUIRED,), (T_PENDING,), (T_HALTED, T_ACQUIRED, T_PENDING)]))
    def settle_what_a_running_task_awaits(self, i, state, who):
        """A settlement fans out to every awaiter whatever state its task is
        in: a suspended one is woken and dispatched, a running or halted one
        only records the resume. Four steps of setup, which is why the
        fan-out targets get a rule of their own rather than being left to
        luck."""
        pool = self._awaited_by(*who) or self._awaited_by(T_HALTED, T_ACQUIRED, T_PENDING)
        self.do(PromiseSettle(self._pick(pool, i).id, state, Value()))

    # ── the catalogue's state half ────────────────────────────────────────

    @invariant()
    def the_state_properties_hold(self):
        assert P.state_failures(self.now, self.s) == [], P.state_failures(self.now, self.s)

    @invariant()
    def the_kernels_own_invariants_hold(self):
        assert check_invariants(self.s.doc) is None, check_invariants(self.s.doc)


KernelMachine.TestCase.settings = settings(
    max_examples=300,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

TestKernelMachine = KernelMachine.TestCase

#: The transitions a campaign must reach for it to have meant anything: it
#: built tasks, drove them through their states, and settled promises that
#: someone was waiting on. The rarer ones — a wake, a halted awaiter's
#: buffered resume, a lease expiry — are asserted exhaustively by
#: `test_explore.py`, not sampled here.
REQUIRED = [
    "born_pending", "born_acquired", "acquired", "suspended", "settled", "expired",
    "retry", "ok", "refused", "execute", "unblock",
]


def test_the_campaign_reaches_every_interesting_transition():
    """Run the machine, then assert the run was not vacuous. A campaign that
    only ever knocked on doors proves nothing, and the specification makes the
    same demand of its own corpus.

    `TestKernelMachine` above is the same machine run for its own sake: it is
    the search that looks for bugs, and it found one."""
    SEEN.clear()
    run_state_machine_as_test(
        KernelMachine,
        settings=settings(max_examples=400, stateful_step_count=40, deadline=None,
                          suppress_health_check=list(HealthCheck)),
    )
    missing = [k for k in REQUIRED if SEEN[k] == 0]
    assert not missing, (missing, dict(SEEN))
