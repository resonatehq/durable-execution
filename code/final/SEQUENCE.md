# The Cloud Run function, in sequence

One route, one engine, and one rule about the order effects happen in.
Everything below is what `server.py`, `engine.py` and `worker.py` actually
do; where a diagram says a name, it is the name in the code.

*These are drawn by hand, from what the design says the system does.*

## Who is who

Only two of these lifelines are somewhere else. Everything in a shaded box
is an object inside **one Cloud Run container**, drawn on its own line
because it is worth seeing separately, not because a message to it crosses
anything.

| lifeline | what it actually is |
|---|---|
| **Cloud Tasks** | the queue, and the only thing that sends `execute` and `timeout` messages. A deadline and a dispatch are both tasks in it |
| **GCS** | one object per origin, at `wf/{origin}`. The whole state of a run |
| `handler` / `Server` | `server.py`. The HTTP entry point: `serve()` in `main.py` builds one `Server` per container at import |
| `Worker` | `worker.Worker` — **an object, not a service**. `Server.worker`, built beside the engine in the same container and called in-process. `run` claims the task and decides what the run meant, `_attempt` runs the function from the top |
| `@resonate research` | the user's own function, running under `asyncio.run` inside `Worker._attempt`. Ordinary async Python that mentions no promise, task or lease |
| `Engine.process` | `engine.Engine`, in the same container again. The only thing that does I/O |
| `kernel` | `handle_external` / `handle_internal`. A pure function: a document in, effects out |
| `store_gcp`, `queue_gcp` | the two adapters, in-process clients for the two services that are not |

A worker being an object rather than a process is the part worth pausing
on. Cloud Tasks is push-only, so nothing here polls for work; a delivery
arrives as an HTTP request, the handler hands it to the outer half, and
the worker's whole life is that one call. Two "workers" running at once
are two containers, each with its own `Server`, sharing nothing but the
bucket.

---

## 1. What the function is

Cloud Tasks is push-only, so a worker is not a loop, it is an endpoint.
That single fact decides the shape: every arrow into the function is an
HTTP POST, including the ones the function sent itself.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant Q as Cloud Tasks
    participant F as Cloud Run<br/>handler()
    participant S as GCS

    Note over F: one Server per container,<br/>built at import from the environment

    C->>+F: POST / — a protocol request
    F->>S: one conditional write
    F-->>-C: 200 {head, data}

    Q->>+F: POST / {kind: execute} — a dispatch
    F-->>-Q: 200 {outcome}

    Q->>+F: POST / {kind: timeout} — a deadline
    F-->>-Q: 200 {timeout}

    Note over Q,F: execute and timeout carry an OIDC token<br/>for ROUTES_ACCOUNT. Protocol requests do not:<br/>a client is not the queue, and whatever fronts<br/>the service protects it instead.
```

---

## 2. One request, in full

This is the whole of `Engine.process`, and the numbered effects are the
only interesting part. A committed document whose deadline was never armed
is the one state nothing repairs, so the arm goes first; a message that
outran its commit would be a consequence of an intention rather than of
state, so the send goes last.

```mermaid
sequenceDiagram
    autonumber
    actor C as Caller
    box rgba(128,128,128,0.08) one Cloud Run container
        participant H as handler / Server
        participant E as Engine.process
        participant K as kernel (pure)
    end
    participant S as GCS<br/>via store_gcp
    participant T as Cloud Tasks<br/>via queue_gcp

    C->>+H: POST /, {kind, data}
    H->>H: authorized() — OIDC, for the queue's routes
    H->>H: parse_request() — a 400 here never reaches the kernel
    H->>+E: process(req, now)

    E->>S: get("wf/{origin}")
    S-->>E: body, generation
    E->>E: decode, now = max(now, doc.clock)

    E->>+K: handle_external(doc, req, now, cfg)
    Note over K: sweeps every expired promise first,<br/>then decides, then merges the two<br/>into one timer transition
    K-->>-E: [SetTimeout, SetDocument, DelTimeout, Send], reply

    alt the objects and the deadline are unchanged
        Note over E,S: the write law: nothing is written at all,<br/>so a read costs no conditional write
    else a transition
        E->>T: 1. arm — create("/", {kind: timeout, origin}, not_before=at)
        T-->>E: task name, recorded in the document
        E->>S: 2. commit — put("wf/{origin}", bytes, if_match=generation)
        S-->>E: new generation
        E->>T: 3. disarm — delete(the name the predecessor armed)
        E->>T: 4. send — create(worker url, execute message)
    end

    E-->>-H: Reply
    H-->>-C: 200 {head, data}
```

### When a step does not answer

```mermaid
sequenceDiagram
    autonumber
    participant E as Engine.process
    participant S as GCS<br/>via store_gcp
    actor C as Caller

    E->>S: put(..., if_match=generation)

    alt 412 — the state moved
        S-->>E: PreconditionFailed
        Note over E: nothing was written. The decision was<br/>made against a document that no longer<br/>exists, so it must be re-decided.
        E-->>C: 409 — ask again, never replay
    else 429 / 503 — no answer
        S-->>E: Unavailable
        Note over E: nothing is known about whether it landed
        E-->>C: 503 — retry, every operation is idempotent
    end
```

The engine never loops on either. A loop here would pick a retry policy —
how many times, how long, whether a re-decided request is the same request
— before anything has said what it should be.

---

## 3. `execute` — a worker running to its block

The outer half of post 002, in the protocol's own words. The function is
run from the top every time; what stops it running twice is not memory but
the promise each call creates at its own position.

```mermaid
sequenceDiagram
    autonumber
    participant Q as Cloud Tasks
    box rgba(128,128,128,0.08) one Cloud Run container — every arrow inside is a Python call
        participant H as handler
        participant W as Worker
        participant Fn as @resonate research
        participant E as Engine
    end

    Q->>+H: POST / {kind: execute, taskId, version}
    H->>+W: run(id, version)
    W->>E: task.acquire(id, version, pid, ttl)

    alt somebody else holds it, or it has moved on
        E-->>W: 4xx
        W-->>H: "not mine"
        Note over H,Q: still a 200. Delivering this again<br/>would not change the answer, and that<br/>refusal is the fence doing its job.
    else acquired at version v
        E-->>W: 200 — the task, and the promise carrying the param
        W->>+Fn: asyncio.run(invoke(*args))

        Fn->>E: task.fence(promise.create "research.1:1")
        E-->>Fn: pending — so run it
        Fn->>Fn: await agent(...)
        Fn->>E: task.fence(promise.settle "research.1:1" resolved)

        Note over Fn: gather() dispatches every branch<br/>before anything blocks
        Fn->>E: task.fence(promise.create "research.1:2", target=search)
        Fn->>E: task.fence(promise.create "research.1:3", target=search)
        E-->>Fn: pending — each create dispatches its own task
        Fn-->>-W: Blocked(["research.1:2", "research.1:3"])

        W->>E: task.suspend(id, v, awaited)
        alt 300 — one settled while we were deciding to wait
            E-->>W: 300
            W->>Fn: run again from the top
        else 200
            E-->>W: 200
            W-->>H: "suspended"
        end
    end

    H-->>-Q: 200 {outcome}
```

Every write the running function makes goes through `task.fence`, at the
version it acquired. A worker that lost its lease cannot write: the fence
refuses, the SDK raises `LeaseLost`, and the attempt hands the task back
rather than settling anything.

The other three ways out of the loop:

| the function | what the worker sends | outcome |
|---|---|---|
| returned | `task.fulfill` resolved | `"done"` |
| raised something of its own | `task.fulfill` **rejected** — a rejection is a result, recorded once and read back on replay | `"rejected"` |
| hit a platform failure (`LeaseLost`, `Conflict`, `Unavailable`) | `task.release` at the same version | `"released"` |

---

## 4. `timeout` — a deadline coming due

The only message no client can send. It is the same `process`, consulting
`handle_internal` instead, and it is idempotent: a duplicate finds nothing
due and the write law stops it writing.

```mermaid
sequenceDiagram
    autonumber
    participant Q as Cloud Tasks
    box rgba(128,128,128,0.08) one Cloud Run container
        participant H as handler
        participant E as Engine.process
        participant K as kernel
    end
    participant S as GCS

    Q->>+H: POST / {kind: timeout, origin}
    H->>+E: process(Timeout(origin), now)
    E->>S: get("wf/{origin}")
    E->>+K: handle_internal(doc, now, cfg)
    Note over K: expire leases → re-pend those tasks<br/>reject promises past their timeout<br/>resolve the ones tagged as timers<br/>re-arm to the next deadline
    K-->>-E: effects

    alt nothing was due
        Note over E,S: no write, no message, nothing
    else something expired
        E->>Q: arm the next deadline
        E->>S: put(if_match) — one write for the whole sweep
        E->>Q: disarm the old deadline
        E->>Q: dispatch whatever was re-pended
    end
    E-->>-H: Reply
    H-->>-Q: 200 {timeout}
```

A dropped `execute` is recoverable: the task's retry deadline was
committed before the message left. A dropped `timeout` is not, because the
deadline it carried is the only thing that was going to fire. A deployment
owes this either a generous retry policy or a periodic sweep over the
bucket that depends on no single queued task — `test_queue.py` demonstrates
the hole and the remedy beside it.

---

## 5. A whole run, across four deliveries

Nothing here is a session. Each box is a separate HTTP request to a
container that may never have seen this run before, and everything one of
them knows it read out of one object in the bucket.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant Q as Cloud Tasks
    participant F as handler
    participant S as GCS<br/>wf/research.1

    C->>F: POST / promise.create "research.1", target=agent
    F->>S: put(if_absent) — the run exists
    F->>Q: create(agent url, execute research.1)
    F-->>C: 200 pending

    Q->>F: POST / execute research.1
    Note over F: acquire → run → the fan-out's<br/>creates dispatch three searches<br/>→ Blocked → suspend
    F->>S: put(if_match) — suspended, awaiting three
    F->>Q: create(search url, execute) ×3

    par three searches, three containers
        Q->>F: POST / execute research.1:2
        F->>S: put — resolved
    and
        Q->>F: POST / execute research.1:3
        F->>S: put — resolved
    and
        Q->>F: POST / execute research.1:4
        F->>S: put — resolved, and this one was last
    end
    Note over F,S: the last settlement consumes the parent's<br/>callback and re-pends its task, in the same write

    F->>Q: create(agent url, execute research.1)
    Q->>F: POST / execute research.1
    Note over F: runs from the top again. Every call<br/>before the block reads its promise back<br/>instead of running: the model is prompted<br/>twice for the whole run, never three times.
    F->>S: put — resolved
    F->>Q: create(listener url, unblock) — for whoever asked
```

The three searches race for the parent's callback and exactly one of them
gets it, because consuming it and settling the promise are the same
conditional write. That is the entire concurrency story.
