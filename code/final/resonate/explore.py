"""Bounded exhaustive exploration of the kernel.

Breadth-first from the empty document over a finite alphabet: every request
the document makes sensible, a timeout, and a few clock advances. States are
deduplicated by a canonical key, so the search covers the reachable set
rather than sampling it, which is what the specification's own sweep does at
script length three (`04-theorems/properties-check.lean`); this goes deeper.

At every edge the whole catalogue runs: a request edge is checked as its two
abstract halves, the sweep as an internal step and the operation as an
external step, and a timeout edge as an internal step. Any failure stops the
search with the path that reached it.

    python explore.py --depth 5          # states and edges per depth, then the tally
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from dataclasses import dataclass
from itertools import combinations

from . import properties as P
from .kernel import (
    Document, KernelCfg, PENDING, REJECTED, RESOLVED, Send, SetDocument,
    T_ACQUIRED, T_HALTED, T_PENDING, T_SUSPENDED, check_invariants,
    handle_external, handle_internal,
)
from .types import (
    Execute, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, TaskAcquire, TaskContinue,
    TaskCreate, TaskFence, TaskFulfill, TaskGet, TaskHalt, TaskHeartbeat,
    TaskRelease, TaskSuspend, Unblock, Value,
)

W = "http://w"
CFG = KernelCfg(retry_timeout=100)


@dataclass(frozen=True)
class Alphabet:
    """What the search may say. Breadth and depth trade against each other,
    so there are two: `BROAD` says many things a few steps deep, `NARROW`
    says few things far enough to reach the long chains (acquire, suspend,
    settle, wake, halt, continue), which no broad search gets to."""

    ids: tuple
    tags: tuple
    timeouts: tuple
    clock: tuple
    #: Whether a deadline is `now + timeout` or the timeout itself. Absolute
    #: deadlines fall into the past as the clock advances, which is how the
    #: born-dead and expiry shapes are reached; relative ones outlive the
    #: script, which is what a long chain needs.
    relative: bool = False


BROAD = Alphabet(
    ids=("o", "o:1", "o:2"),
    tags=({"resonate:target": W}, {"resonate:scope": "global"}, {"resonate:timer": "true"},
          {"resonate:target": W, "resonate:delay": "60"}),
    timeouts=(50, 1_000),
    clock=(5, 100, 1_000),
)

NARROW = Alphabet(
    ids=("o", "o:1"),
    tags=({"resonate:target": W}, {"resonate:scope": "global"}),
    timeouts=(100_000,),
    clock=(100,),
    relative=True,
)


class Timeout:
    """The internal message: a deadline for the origin came due."""

    __slots__ = ()

    def __repr__(self):
        return "Timeout()"


class Advance:
    __slots__ = ("by",)

    def __init__(self, by):
        self.by = by

    def __repr__(self):
        return f"Advance({self.by})"


def actions(now, doc, ab=BROAD):
    """Every action the alphabet allows from this document. Requests that
    can only be refused (a wrong version, an id with no task) are left out:
    they lead back to the same state and the walk already knocks on doors."""
    out = [Timeout()] + [Advance(d) for d in ab.clock]
    for id in ab.ids:
        for tags in ab.tags:
            for to in ab.timeouts:
                out.append(PromiseCreate(id, now + to if ab.relative else to, Value(), dict(tags)))
        out.append(TaskCreate("p1", 10, PromiseCreate(id, now + 1_000 if ab.relative else 1_000, Value(), {"resonate:target": W})))
    for o in doc.objects:
        p, t, id = o.promise, o.task, o.id
        out.append(PromiseGet(id))
        if p.state == PENDING:
            out.append(PromiseSettle(id, RESOLVED, Value(data="v")))
            out.append(PromiseSettle(id, REJECTED))
            if p.is_external():
                out.append(PromiseRegisterListener(id, "http://l"))
                for q in doc.objects:
                    if q.id != id and q.task is not None:
                        out.append(PromiseRegisterCallback(id, q.id))
        if t is None:
            continue
        out.append(TaskGet(id))
        if t.state == T_PENDING:
            out += [TaskAcquire(id, t.version, pid, 10) for pid in ("p1", "p2")]
            out.append(TaskHalt(id))
        if t.state == T_ACQUIRED:
            out.append(TaskRelease(id, t.version))
            out.append(TaskFulfill(id, t.version, PromiseSettle(id, RESOLVED, Value(data="v"))))
            out.append(TaskHeartbeat(t.pid, ((id, t.version),)))
            out.append(TaskHalt(id))
            others = [q.id for q in doc.objects if q.id != id and q.promise.is_external()]
            for k in (1, 2):
                for sub in combinations(others, k):
                    out.append(TaskSuspend(id, t.version, sub))
            for other in ab.ids:
                if other != id:
                    out.append(TaskFence(id, t.version, "c", PromiseCreate(other, now + 1_000 if ab.relative else 1_000, Value(), {"resonate:target": W})))
                    out.append(TaskFence(id, t.version, "c", PromiseSettle(other, RESOLVED)))
        if t.state == T_SUSPENDED:
            out.append(TaskHalt(id))
        if t.state == T_HALTED:
            out.append(TaskContinue(id))
    return out


def key(now, s):
    """The canonical identity of a state: the clock, every object field, and
    the outbox by key."""
    objs = []
    for o in s.doc.objects:
        p, t = o.promise, o.task
        objs.append((
            o.id, p.state, p.param.data, p.value.data, tuple(sorted(p.tags.items())),
            p.timeout_at, p.created_at, p.settled_at, tuple(p.callbacks), tuple(p.listeners),
            None if t is None else (t.state, t.version, t.pid, t.ttl, tuple(sorted(t.resumes)), t.retry_at, t.lease_at),
        ))
    outbox = tuple(sorted((P.outbox_key(e), e.address, repr(e.msg)) for e in s.outbox))
    return (now, tuple(objs), outbox)


class Violation(Exception):
    pass


def step(now, s, action, tally, ab=BROAD):
    """One edge, checked. Returns (now', state') or raises Violation."""
    if isinstance(action, Advance):
        return now + action.by, s
    swept = handle_internal(s.doc, now, CFG)
    mid = s.after(next(e.doc for e in swept if isinstance(e, SetDocument)), [e for e in swept if isinstance(e, Send)])
    bad = check_invariants(mid.doc)
    if bad or P.state_failures(now, mid) or P.trans_failures(now, s, mid) or P.internal_failures(now, s, mid):
        raise Violation(("sweep", bad, P.state_failures(now, mid), P.trans_failures(now, s, mid), P.internal_failures(now, s, mid)))
    if isinstance(action, Timeout):
        tally_edge(s, mid, [e for e in swept if isinstance(e, Send)], None, tally, internal=True)
        return now, mid
    fx, reply = handle_external(mid.doc, action, now, CFG)
    doc = next(e.doc for e in fx if isinstance(e, SetDocument))
    sends = [e for e in fx if isinstance(e, Send)]
    nxt = mid.after(doc, sends)
    bad = check_invariants(doc)
    if bad or P.state_failures(now, nxt) or P.trans_failures(now, mid, nxt):
        raise Violation(("request", bad, P.state_failures(now, nxt), P.trans_failures(now, mid, nxt)))
    fused, _ = handle_external(s.doc, action, now, CFG)
    if next(e.doc for e in fused if isinstance(e, SetDocument)) != doc:
        raise Violation(("fused != composed",))
    tally_edge(mid, nxt, sends, reply, tally, internal=False)
    return now, nxt


def tally_edge(a, b, sends, reply, tally, internal):
    for o in b.doc.objects:
        x = a.doc.get(o.id)
        if x is None:
            tally["born_dead" if o.promise.state != PENDING else "born_pending"] += 1
            if o.task is not None and o.task.state == T_ACQUIRED:
                tally["born_acquired"] += 1
            if o.task is not None and o.task.state == T_PENDING and not sends:
                tally["delayed"] += 1
            continue
        if x.task is not None and o.task is not None:
            if x.task.state == T_SUSPENDED and o.task.state == T_PENDING:
                tally["wake"] += 1
            if x.task.state == T_ACQUIRED and o.task.state == T_PENDING and internal:
                tally["lease_expired"] += 1
            if x.task.state == T_PENDING and o.task.state == T_PENDING and x.task.retry_at != o.task.retry_at:
                tally["retry"] += 1
            if o.task.state == T_HALTED and o.task.resumes - x.task.resumes:
                tally["halted_buffer"] += 1
        if x.promise.state == PENDING and o.promise.state != PENDING and o.promise.settled_at == o.promise.timeout_at:
            tally["expired"] += 1
    if reply is not None and reply.status == 300:
        tally["carry_on_300"] += 1
    if reply is not None and reply.status >= 400:
        tally["refused"] += 1
    tally["unblock"] += sum(isinstance(e.msg, Unblock) for e in sends)
    tally["execute"] += sum(isinstance(e.msg, Execute) for e in sends)


def explore(depth, limit=None, log=None, ab=BROAD, visit=None):
    """Breadth-first to `depth`. Returns (states per depth, edges, tally).

    `visit(now, doc)` is called once per state first reached, which is how
    something other than the catalogue — the line schema, say — gets handed
    every document the kernel can produce rather than the few a script
    happens to build."""
    start = (0, P.State(Document(), retry_timeout=CFG.retry_timeout))
    seen = {key(*start): 0}
    if visit is not None:
        visit(*(start[0], start[1].doc))
    frontier = deque([(start, [])])
    per_depth = [1]
    edges = 0
    tally = {k: 0 for k in ("born_pending", "born_dead", "born_acquired", "delayed", "wake", "lease_expired",
                            "retry", "halted_buffer", "expired", "carry_on_300", "refused", "unblock", "execute")}
    for d in range(depth):
        nxt = deque()
        while frontier:
            (now, s), path = frontier.popleft()
            for action in actions(now, s.doc, ab):
                edges += 1
                try:
                    now2, s2 = step(now, s, action, tally, ab)
                except Violation as v:
                    raise Violation((v.args[0], path + [action])) from None
                k = key(now2, s2)
                if k not in seen:
                    seen[k] = d + 1
                    if visit is not None:
                        visit(now2, s2.doc)
                    nxt.append(((now2, s2), path + [action]))
                    if limit and len(seen) >= limit:
                        frontier = nxt
                        per_depth.append(len(nxt))
                        return per_depth, edges, tally
        frontier = nxt
        per_depth.append(len(nxt))
        if log:
            log(f"depth {d + 1}: {len(nxt)} new states, {len(seen)} total, {edges} edges")
    return per_depth, edges, tally


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="stop after this many states")
    ap.add_argument("--alphabet", choices=["broad", "narrow"], default="broad")
    args = ap.parse_args()
    t0 = time.time()
    per_depth, edges, tally = explore(args.depth, args.limit, log=lambda m: print(m, file=sys.stderr),
                                      ab={"broad": BROAD, "narrow": NARROW}[args.alphabet])
    print(f"{sum(per_depth)} states, {edges} edges, {time.time() - t0:.1f}s")
    for k, v in tally.items():
        print(f"  {k:14} {v}")
