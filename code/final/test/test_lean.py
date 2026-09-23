"""The kernel against its Lean transcription, step for step.

`lean/` holds `kernel.py` transcribed into Lean 4 and the theorems proved
about that transcription. A theorem about a transcription is only a theorem
about this kernel to the extent the two are the same function, and this is
the test that says they are: random scripts, the same ones through both,
every reply, every effect in order and every committed document required to
be equal.

The scripts are walks. Each step is drawn from what `explore.actions` offers
from the current document — the requests that do something — or, one time in
four, from `knocks`, the requests that must be refused: stale versions, ids
nobody created, malformed tags, bad addresses. Both alphabets are walked,
the broad one for variety and the narrow one for the long chains.

Skipped when there is no `lake` on the path. `LEAN_SCRIPTS` sets how many
scripts per alphabet (default 300); `LEAN_SEED` pins the seed.

    cd lean && lake build            # once, and after every change to lean/
    python -m pytest test/test_lean.py
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

import explore as X
from kernel import (
    Document, Execute, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, SetDocument, SetTimeout, DelTimeout, Send,
    TaskAcquire, TaskContinue, TaskCreate, TaskFence, TaskFulfill, TaskGet, TaskHalt,
    TaskHeartbeat, TaskRelease, TaskSuspend, Unblock, Value, handle_external, handle_internal,
)

LEAN = Path(__file__).parent.parent / "lean"
LAKE = shutil.which("lake")
pytestmark = pytest.mark.skipif(LAKE is None, reason="no lake on the path")

CFG = X.CFG

# ---------------------------------------------------------------------------
# The encoding, which Main.lean writes too
# ---------------------------------------------------------------------------


def enc_value(v: Value) -> dict:
    return v.to_json()


def enc_create(r: PromiseCreate) -> dict:
    return {"id": r.id, "timeoutAt": r.timeout_at, "param": enc_value(r.param), "tags": dict(r.tags)}


def enc_settle(r: PromiseSettle) -> dict:
    return {"id": r.id, "state": r.state, "value": enc_value(r.value)}


def enc_req(r) -> dict:
    match r:
        case PromiseGet():
            return {"kind": "promise.get", "id": r.id}
        case PromiseCreate():
            return {"kind": "promise.create", **enc_create(r)}
        case PromiseSettle():
            return {"kind": "promise.settle", **enc_settle(r)}
        case PromiseRegisterCallback():
            return {"kind": "promise.register_callback", "awaited": r.awaited, "awaiter": r.awaiter}
        case PromiseRegisterListener():
            return {"kind": "promise.register_listener", "awaited": r.awaited, "address": r.address}
        case TaskGet():
            return {"kind": "task.get", "id": r.id}
        case TaskCreate():
            return {"kind": "task.create", "pid": r.pid, "ttl": r.ttl, "action": enc_create(r.action)}
        case TaskAcquire():
            return {"kind": "task.acquire", "id": r.id, "version": r.version, "pid": r.pid, "ttl": r.ttl}
        case TaskRelease():
            return {"kind": "task.release", "id": r.id, "version": r.version}
        case TaskFulfill():
            return {"kind": "task.fulfill", "id": r.id, "version": r.version, "action": enc_settle(r.action)}
        case TaskSuspend():
            return {"kind": "task.suspend", "id": r.id, "version": r.version, "awaited": list(r.awaited)}
        case TaskFence():
            a = r.action
            action = ({"kind": "promise.create", **enc_create(a)} if isinstance(a, PromiseCreate)
                      else {"kind": "promise.settle", **enc_settle(a)})
            return {"kind": "task.fence", "id": r.id, "version": r.version, "corrId": r.corr_id, "action": action}
        case TaskHeartbeat():
            return {"kind": "task.heartbeat", "pid": r.pid, "tasks": [list(t) for t in r.tasks]}
        case TaskHalt():
            return {"kind": "task.halt", "id": r.id}
        case TaskContinue():
            return {"kind": "task.continue", "id": r.id}
    raise TypeError(r)


def enc_doc(doc: Document) -> dict:
    return {"timerAt": doc.timer_at, "objects": [{
        "id": o.id,
        "promise": {
            "state": o.promise.state, "param": enc_value(o.promise.param), "value": enc_value(o.promise.value),
            "tags": dict(o.promise.tags), "timeoutAt": o.promise.timeout_at, "createdAt": o.promise.created_at,
            "settledAt": o.promise.settled_at, "callbacks": list(o.promise.callbacks),
            "listeners": list(o.promise.listeners),
        },
        "task": None if o.task is None else {
            "state": o.task.state, "version": o.task.version, "pid": o.task.pid, "ttl": o.task.ttl,
            "resumes": sorted(o.task.resumes), "retryAt": o.task.retry_at, "leaseAt": o.task.lease_at,
        },
    } for o in doc.objects]}


def enc_effect(e) -> list:
    match e:
        case SetTimeout():
            return ["setTimeout", e.at]
        case SetDocument():
            return ["setDocument", enc_doc(e.doc)]
        case DelTimeout():
            return ["delTimeout", e.at]
        case Send(msg=Execute()):
            return ["send", e.address, {"execute": [e.msg.task_id, e.msg.version]}]
        case Send(msg=Unblock()):
            return ["send", e.address, {"unblock": e.msg.promise}]
    raise TypeError(e)


def run_python(script: list[tuple[int, object]]) -> list[dict]:
    """The script through `kernel.py`, encoded as Main.lean encodes it."""
    doc, out = Document(), []
    for now, action in script:
        if isinstance(action, X.Timeout):
            fx, reply = handle_internal(doc, now, CFG), None
        else:
            fx, r = handle_external(doc, action, now, CFG)
            reply = {"status": r.status, "data": r.data}
        doc = next(e.doc for e in fx if isinstance(e, SetDocument))
        out.append(json.loads(json.dumps({"effects": [enc_effect(e) for e in fx], "reply": reply})))
    return out


def run_lean(scripts: list[list[tuple[int, object]]]) -> list[list[dict]]:
    """Every script through the Lean kernel, in one process: a blank line
    between scripts starts the next from the empty document."""
    lines = []
    for script in scripts:
        for now, action in script:
            if isinstance(action, X.Timeout):
                lines.append(json.dumps({"now": now, "sweep": True}))
            else:
                lines.append(json.dumps({"now": now, "req": enc_req(action)}))
        lines.append("")
    cfg = json.dumps({"retryTimeout": CFG.retry_timeout, "preloadLimit": CFG.preload_limit})
    proc = subprocess.run(
        [LAKE, "env", "lean", "--run", "Main.lean", cfg],
        input="\n".join(lines) + "\n", capture_output=True, text=True, cwd=LEAN, timeout=3600,
    )
    assert proc.returncode == 0, proc.stderr
    out, cur = [], []
    for line in proc.stdout.split("\n"):
        if line == "":
            out.append(cur)
            cur = []
        else:
            cur.append(json.loads(line))
    return out[: len(scripts)]


# ---------------------------------------------------------------------------
# The scripts
# ---------------------------------------------------------------------------

ADDRESSES = ("http://l", "http://m", "https://x.y/z", "worker://agent", "mailto:a@b",
             "http:", "http://", "http:/x", "http:/\t/x", "HTTP://X", "1http://x", "", "nocolon", "a+b-c.d:rest")


def knocks(now: int, doc: Document, ab: X.Alphabet, rng: random.Random) -> list:
    """Requests that should mostly be refused, and the odd ones that should
    not: the doors `explore.actions` leaves shut on purpose."""
    ids = list(ab.ids) + ["o:3", "x", "x:1", "o.1"]
    objs = doc.objects
    pick = lambda xs: rng.choice(xs) if xs else rng.choice(ids)
    some_id = pick([o.id for o in objs])
    task_ids = [o.id for o in objs if o.task is not None]
    tid = pick(task_ids)
    t = doc.get(tid).task if doc.get(tid) is not None and doc.get(tid).task is not None else None
    v = t.version if t is not None else 0
    wrong = v + rng.choice((1, -1 if v > 0 else 2))
    tags = rng.choice((
        {}, {"resonate:target": rng.choice(ADDRESSES)}, {"resonate:timer": "true", "resonate:target": "http://w"},
        {"resonate:delay": rng.choice(("5", "x", "-1", "999999", "0"))},
        {"resonate:delay": rng.choice(("5", "200", "0")), "resonate:target": "http://w"},
        {"resonate:target": "http://w", "resonate:branch": rng.choice(("b", ""))},
        {"resonate:scope": "global", "resonate:branch": "b"}, {"resonate:external": "true"},
        {"resonate:timer": "true"},
    ))
    value = Value(headers=rng.choice((None, {"h": "1"})), data=rng.choice((None, "v", "")))
    state = rng.choice(("resolved", "rejected", "rejected_canceled", "rejected_timedout", "pending"))
    to = rng.choice((0, now, now + 1, now + 50, now + 10_000))
    create = PromiseCreate(rng.choice(ids), to, value, tags)
    return [
        PromiseGet(rng.choice(ids)),
        create,
        PromiseSettle(some_id, state, value),
        PromiseRegisterCallback(some_id, pick(task_ids)),
        PromiseRegisterCallback(some_id, some_id),
        PromiseRegisterCallback(some_id, "x:1"),
        PromiseRegisterCallback(rng.choice(ids), pick(task_ids)),
        PromiseRegisterListener(some_id, rng.choice(ADDRESSES)),
        TaskGet(rng.choice(ids)),
        TaskCreate(rng.choice(("p1", "p2")), rng.choice((10, 0, -3)), PromiseCreate(some_id, to, value, tags)),
        TaskCreate("p1", 10, PromiseCreate(rng.choice(ids), to, value, {"resonate:target": "http://w", "resonate:branch": "b"})),
        TaskAcquire(tid, rng.choice((v, wrong)), rng.choice(("p1", "p2")), rng.choice((10, 0))),
        TaskRelease(tid, rng.choice((v, wrong))),
        TaskFulfill(tid, rng.choice((v, wrong)), PromiseSettle(rng.choice((tid, some_id)), state, value)),
        TaskSuspend(tid, rng.choice((v, wrong)), tuple(rng.sample(ids + [tid], rng.randint(0, 3)))),
        TaskSuspend(tid, v, (some_id, some_id)),
        TaskFence(tid, rng.choice((v, wrong)), "c", rng.choice((create, PromiseSettle(some_id, state, value)))),
        TaskHeartbeat(rng.choice(("p1", "p2")), tuple((i, rng.choice((v, wrong))) for i in rng.sample(ids, 2))),
        TaskHeartbeat(t.pid if t is not None and t.pid else "p1", ((tid, v),)),
        TaskHalt(pick(task_ids)),
        TaskContinue(pick(task_ids)),
    ]


def walk(rng: random.Random, ab: X.Alphabet, steps: int) -> list[tuple[int, object]]:
    """One script. `now` only moves forward, as the engine's clamp makes it."""
    now, doc, script = 0, Document(), []
    for _ in range(steps):
        if rng.random() < 0.25:
            action = rng.choice(knocks(now, doc, ab, rng))
        else:
            action = rng.choice(X.actions(now, doc, ab))
        if isinstance(action, X.Advance):
            now += action.by
            continue
        script.append((now, action))
        if isinstance(action, X.Timeout):
            fx = handle_internal(doc, now, CFG)
        else:
            fx, _ = handle_external(doc, action, now, CFG)
        doc = next(e.doc for e in fx if isinstance(e, SetDocument))
    return script


def check(scripts):
    lean = run_lean(scripts)
    assert len(lean) == len(scripts)
    for script, got in zip(scripts, lean):
        want = run_python(script)
        for i, (w, g) in enumerate(zip(want, got)):
            assert "error" not in g, (script[: i + 1], g)
            assert g == w, f"step {i} of {script[: i + 1]}:\n lean   {g}\n python {w}"
        assert len(got) == len(want)


@pytest.mark.parametrize("name", ["broad", "narrow"])
def test_python_and_lean_agree_on_random_walks(name):
    ab = {"broad": X.BROAD, "narrow": X.NARROW}[name]
    rng = random.Random(int(os.environ.get("LEAN_SEED", "1")))
    n = int(os.environ.get("LEAN_SCRIPTS", "300"))
    check([walk(rng, ab, rng.randint(1, 30)) for _ in range(n)])


def test_the_driver_can_disagree():
    """A differential that cannot fail proves nothing: a script whose Python
    side is tampered with must be reported."""
    script = [(0, PromiseCreate("o", 100, Value(), {"resonate:target": "http://w"}))]
    got = run_lean([script])[0]
    want = run_python(script)
    assert got == want
    want[0]["effects"][-1][1] = "http://elsewhere"
    assert got != want
