# final — the engine, end to end, in Python, on Google Cloud Storage

This folder is where the engine gets built for real. This document is the plan:
what we learned from the two posts and from `resonatehq/resonate`, and how the
pieces fit end to end. Every non-obvious decision below should grow an entry in
`notes/` as it gets implemented.

## 1. What we are building on

**The programming model (posts 001 and 002 in `design/content/writing`).**
One primitive, the durable promise, with two operations, `create` and `settle`,
both first-writer-wins and therefore idempotent. Calls are memoized by
*position* (`run:1`, `run:2`, `run:2:1`), so replay derives the same id every
time. `durable(store, id, func, *args)` is the runtime frame under every call.
A remote call is the same promise with `create` here and `settle` over there;
the frame raises `Blocked(id)` to unwind the stack, the outer loop
(`execute_until_blocked_outer`) subscribes the task to the promise and releases
it, and a settle later re-queues the task, which is re-run from the top.

**The object-store design (`resonatehq/resonate`).** Two independent
implementations of the same design exist and agree with each other:
`crates/resonate-server-blob` (Rust, on `main`, over the `object_store` crate,
which speaks S3, GCS and Azure alike) and `impl/server/s3` (Zig, on branch
`claude/resonate-s3-zig-5kf2ia`). Both were written against S3; both name GCS
as qualifying, because the design needs exactly one thing from the store: real
conditional writes. We run on GCP, so the store is **GCS**, and both the kernel's `Send` effect
and its deadlines go through **Cloud Tasks**. Nothing above the store port
changes. The design:

- **One document per origin.** Everything before the first `:` of an id is the
  origin; `wf/<origin>` holds every promise and task of that origin. Every
  protocol operation is single-origin, so one conditional write commits a whole
  transition. No log, no lock, no lease object, no consensus.
- **CAS is what GCS gives you.** Every object has a `generation` number.
  `ifGenerationMatch=0` creates only if absent; `ifGenerationMatch=<gen>`
  replaces only what was read. `412` means the state moved: re-read and
  **re-decide**, never replay. `429`/`503` means the bucket is throttling: back
  off and retry the same write. No answer: tell the caller and let it retry,
  every op is idempotent. GCS also enforces roughly **one write per second per
  object**, which is the strongest argument for group commit below.
- **Deadlines are durable objects outside the document.** The reference
  servers write them as zero-byte keys `t/<NN>/<deadline>_<origin>` and poll
  the prefix. We keep the *rule* (arm the deadline before the commit, record
  its name in the document, disarm the old one after) and change the *object*:
  a deadline becomes a Cloud Task scheduled at `deadline` that calls the
  worker's sweep endpoint for that origin. Same crash-window argument, no
  polling loop, no monotone key prefix.
- **The kernel is a pure function.** `handle(doc, req, now) -> (effects, reply)`
  and `drain(doc, now) -> effects`. Effects are `SetTimeout`, `SetDocument`,
  `DelTimeout`, `Send`. Performed in that fixed order: arm the new deadline,
  CAS the document, delete the old deadline, send, answer. Every crash window
  between two of those leaves a state something repairs.
- **One actor per origin, group commit.** Requests enqueue; the actor drains
  its mailbox and folds the batch through the kernel, so a hot origin costs one
  CAS per batch. The "write law": if promises, tasks and `timer_at` are
  byte-equal after the decision, nothing is written.
- **Canonical encoding.** One JSON line per entity, fixed key order, omit-empty,
  ASCII only, so state-equal means byte-equal and a lost-response retry can
  recognise its own landed write.

**The precedent for "no server process".** The Python SDK on that branch ships
`resonate-pg`: a connector that is both `Network` (request/reply) and `Source`
(execute/unblock messages), so the SDK talks to Postgres directly and "the
server *is* the database". We do the same with GCS: the SDK talks to the
bucket directly, and the kernel runs inside the worker. Resonate's `transport_http_push`
is the shape a Cloud Tasks delivery lands in: an HTTP POST carrying the
execute message to a worker URL, so the worker side is an established shape
too.

## 2. The system, end to end

```
 @resonate / durable() / .rpc / gather          sdk/       the posts, made real
 ────────────────────────────────────────────────────────────────────────────
 execute_until_blocked_outer / inner            worker.py  claim, run, fulfill|suspend|release, heartbeat
 ────────────────────────────────────────────────────────────────────────────
 Applier   load → decide → arm → CAS → disarm → send     applier.py  one actor per origin
 Kernel    handle(doc, req, now), drain(doc, now)        kernel.py   pure
 Doc       OriginDoc + canonical codec                   doc.py
 Timers    arm(origin, at) → task name; disarm(name)     timers.py   Cloud Tasks, or an in-process heap
 Transport send(execute, target, not_before)            transport.py Cloud Tasks, or in-memory
 ────────────────────────────────────────────────────────────────────────────
 Store     get / put_if_match / put_if_none_match /     store.py    MemoryStore, GcsStore
           put / delete / list(prefix, max_keys)
```

### store.py — the object-store port

Six operations, a `Version` (the GCS generation), and a three-way error:
`PreconditionFailed`, `Throttled`, `Unavailable`. `MemoryStore` implements the
same CAS semantics in a dict and drives every test. `GcsStore` wraps
`google-cloud-storage` (sync, run under `asyncio.to_thread`; swap for
`gcloud-aio-storage` if the thread pool shows up in profiles) with
`if_generation_match` on every write, `if_generation_not_match` on cached
reads (a `304` costs no body), and maps `412` / `429` / everything else onto
the three errors. Optional `FaultStore` that fails after N writes, to cut the
power between two effects.

### doc.py — the origin document

```python
@dataclass
class Promise:  id, state, param, value, tags, timeout_at, created_at, settled_at, callbacks, listeners
@dataclass
class Task:     id, state, version, pid, ttl, resumes, retry_at, lease_at
@dataclass
class OriginDoc: promises: dict, tasks: dict, clock: int, gen: int, timer_at: int | None
```

`encode(doc) -> bytes` and `decode(bytes) -> OriginDoc`, canonical, with a
header line binding the document to its key. `min_deadline(doc)` derives
`timer_at`.

### kernel.py — the state machine, pure

`handle(doc, req, now, cfg) -> (OriginDoc, [Effect], Reply)` over the
operations we actually need:

| op | meaning in the posts' vocabulary |
|---|---|
| `promise.create` / `promise.settle` / `promise.get` | `store.create`, `store.settle`, and the read spelled as a create |
| `promise.register_callback(awaited, awaiter)` | `store.subscribe(b.id, task)` |
| `task.acquire(id, version, pid, ttl)` | `queue.claim()` with a lease |
| `task.heartbeat` | keep the lease |
| `task.suspend(id, version, awaited...)` | `store.subscribe` + `queue.release` in one atomic step |
| `task.fulfill(id, version, state, value)` | settle the run's own promise + `queue.complete` |
| `task.release` | give the claim back on error |

A task's id *is* its root promise's id; `version` is the fencing token. A
settle fans out to the promise's callbacks: each awaiting task drops the id from
`resumes`, and when `resumes` is empty the task goes back to `pending` with a
`Send(execute)` effect. `drain(doc, now)` settles expired promises, re-dispatches
tasks past `retry_at`, and reclaims tasks past `lease_at`.

### applier.py — load, decide, perform

Per origin: an `asyncio.Lock` (single process) fronting a mailbox. `submit`
loads `(bytes, etag)` from cache or store, folds the batch through `handle`,
applies the write law, then performs effects in order. `412` drops the cache
entry, reloads, re-decides, up to `max_cas_retries`. `409` retries the same
write once. `tick(origin, now)` does the same with `drain`.

### timers.py — deadlines as Cloud Tasks

The port is two calls: `arm(origin, at) -> name` and `disarm(name)`. The Cloud
Tasks implementation creates a task with `schedule_time = at` whose HTTP
target is the worker's `POST /sweep/<origin>`, and returns the task name,
which the document records as `timer_name` next to `timer_at`. Disarm deletes
by that name and ignores "not found". The task is **unnamed** on creation (the
service picks the name): a caller-chosen name has a tombstone after deletion,
so re-arming the same `(origin, deadline)` within the hour would be refused,
which is the exact trap the Zig server's per-arm token exists to avoid.

Firing is at-least-once over an idempotent sweep: the handler calls
`applier.tick(origin, now)`, which runs `drain`, and returns 2xx whether or
not anything was due. An orphan from a crash between arm and commit fires
into a no-op. Two limits to design around: Cloud Tasks schedules at most 30
days out, so `arm` clamps to `min(at, now + 30d)` and a sweep that finds
nothing due simply re-arms; and a queue dispatches a bounded rate, so timers
and executes go on separate queues.

The in-process implementation is a heap plus `asyncio.sleep`, and drives every
test over `MemoryStore`.

### transport.py — the kernel's `send`, as a Cloud Task

The `send(target, id, args)` of post 002 is the kernel's `Send(execute)`
effect, and on GCP it *is* a Cloud Task. The port is `send(msg, target,
not_before=None)`. The Cloud Tasks implementation creates a task on the
target group's queue with an HTTP target of the group's `POST /execute` URL,
body `{task id, version}`, and `schedule_time = not_before` when the message
carries a delay (`resonate:delay`, a durable sleep, a retry backoff). One
mechanism covers immediate and deferred delivery, which is what Pub/Sub could
not do.

Delivery is at-least-once with Cloud Tasks' own retries, which the protocol
already tolerates: the handler calls `task.acquire`, the CAS on the document
decides who wins, and a refused acquire still returns 2xx so the task is not
retried. A message lost between commit and the create call is re-sent by the
task's `retry_at` deadline, which the commit already carries.

Cloud Tasks is push-only, so a worker is an HTTP service: on GCP that is Cloud
Run, with the two routes Cloud Tasks calls (`/execute`, `/sweep`) and nothing
else. A run that outlives the request's dispatch deadline is fine: the lease
and heartbeat cover it, and the retry Cloud Tasks sends is refused by the
lease.

If the target is this process and there is no delay, the message is handed
over in memory and nothing leaves the process; the in-memory transport is what
tests use.

### worker.py and sdk/ — the posts, made real

`worker.py` is `execute_until_blocked_outer` behind two HTTP routes:
`/execute` acquires, heartbeats, runs the function from the top, then
`fulfill`s on return, `suspend`s on `Blocked`, or `release`s on an unexpected
error; `/sweep/<origin>` ticks the applier. In tests the same object is called
directly, no HTTP. `sdk/` is `@resonate`, `durable()` with
positional ids from a contextvar, `.rpc` which creates the callee's promise
with a `resonate:target` tag so the kernel dispatches it, and `gather`, which
collects every `Blocked` id and suspends on all of them at once. Nothing in
`sdk/` knows about the bucket or Cloud Tasks; it calls `applier.submit`.

## 3. Order of work

1. **`store.py`, `doc.py`, `kernel.py` over `MemoryStore`.** Unit tests for
   first-writer-wins, fencing, fan-out, timeouts. Every listing from post 001
   runs against this.
2. **`applier.py`, `timers.py`, `transport.py` in memory.** Crash-window tests
   with `FaultStore`: kill after arm, after CAS, after disarm, after send;
   assert each is repaired.
3. **`sdk/` and `worker.py`.** The research agent from the repo README runs end
   to end in one process, is killed at random points, and resumes.
4. **Two processes over HTTP.** Two workers as HTTP services over
   `MemoryStore`, with a small in-process stand-in for Cloud Tasks (an HTTP
   client with a delay heap), so `rpc` and durable sleep cross the process
   boundary before any GCP credential is involved. There is no Cloud Tasks
   emulator worth trusting, so the stand-in is ours and is held to the same
   port tests as the real one.
5. **`GcsStore` and real Cloud Tasks.** Live test against a real bucket and
   two real queues: `fake-gcs-server` honours generation preconditions well
   enough for CI, but the claim "it works on GCS" is only true once it has run
   against GCS. Two Cloud Run workers, one bucket, randomized traffic, and a
   snapshot diff against the Rust `resonate-server-blob` in-memory server on
   the same requests, so our semantics are held to theirs.

Line budget, first estimate: store 250, doc 300, kernel 900, applier 300,
timers 150, transport 150, worker 250, sdk 400, roughly 2,700 for the engine
and the rest for tests.

## 4. Decisions to make

- **Wire vocabulary.** Speak Resonate's envelope (`{"kind": "task.acquire",
  "head": {...}, "data": {...}}`) so the differential test in step 5 is free,
  or the posts' smaller vocabulary and translate only in the test. Recommended:
  Resonate's names for the kernel's `Req` types, the posts' names in `sdk/`.
- **Multiple processes on one origin.** The per-origin actor serializes writers
  inside one process; across processes the CAS does. Cache reads must be
  revalidated with `if_generation_not_match` unless a `sole_writer` flag says
  otherwise. GCS's one-write-per-second-per-object limit means a hot origin
  shared by many processes will throttle; group commit inside each process is
  the mitigation, and a per-origin owner is the escalation if it is not enough.
- **One queue or two.** Executes and sweeps on separate Cloud Tasks queues,
  so a burst of work cannot starve deadlines. Per-group execute queues if
  groups need independent rate limits.
- **Authentication of the two endpoints.** Cloud Tasks can sign requests with
  an OIDC token for a service account; the worker verifies it and accepts
  nothing else. Decide whether the worker is public-with-OIDC or reachable
  only from the VPC.
- **The 30-day clamp.** A promise timeout further out than Cloud Tasks can
  schedule is re-armed on each no-op sweep. Cheap, but it is a place a bug
  could make a promise never time out, so it gets its own test.
- **Search and observability.** A search reads every document. Off by default,
  as in both reference servers.
