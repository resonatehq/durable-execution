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
conditional writes. We run on GCP, so the store is **GCS** and the transport is
**Pub/Sub**, and nothing above the store port changes. The design:

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
- **Deadlines are keys.** `t/<NN>/<20-digit deadline>_<origin>` as zero-byte
  objects; GCS lists lexicographically, so a capped LIST returns the nearest
  deadlines. `NN` shards a monotone key space, which GCS's own guidance flags as
  the pattern to avoid. A timer write is unconditional because the key *is*
  the record.
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
bucket directly, and the kernel runs inside the worker. Resonate also already
delivers messages as `gcps://<project>/<topic>` (`resonate-transport-gcps`), so
Pub/Sub as the transport is an established shape, not a new one.

## 2. The system, end to end

```
 @resonate / durable() / .rpc / gather          sdk/       the posts, made real
 ────────────────────────────────────────────────────────────────────────────
 execute_until_blocked_outer / inner            worker.py  claim, run, fulfill|suspend|release, heartbeat
 ────────────────────────────────────────────────────────────────────────────
 Applier   load → decide → arm → CAS → disarm → send     applier.py  one actor per origin
 Kernel    handle(doc, req, now), drain(doc, now)        kernel.py   pure
 Doc       OriginDoc + canonical codec                   doc.py
 Timerd    list t/ ascending, sweep due origins          timerd.py
 Transport publish(execute) / pull per group            transport.py Pub/Sub, or a bucket-only inbox
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

### timerd.py — deadlines

A loop that LISTs each `t/<NN>/` shard ascending with a small cap, parses
`(deadline, origin)` from each key, calls `applier.tick(origin, now)` for the
due ones, and deletes the key after the sweep. At-least-once firing over an
idempotent sweep. In-process it also keeps a heap of armed deadlines so the
common case costs no LIST.

### transport.py — how a worker finds work

A `Send(execute)` effect names a target. The transport port has two
implementations behind one interface, chosen per deployment:

- **Pub/Sub (default on GCP).** A target is a topic, one per worker group;
  the applier publishes the execute message *after* the CAS commits, and every
  worker in the group pulls from the group's subscription. Delivery is
  at-least-once, which is exactly what the protocol already tolerates: the
  worker calls `task.acquire`, the CAS on the document decides who wins, and
  the loser acks the message and moves on. A message lost between commit and
  publish is re-sent by the task's `retry_at` deadline, which the commit
  already carries. Pub/Sub push subscriptions make this deployable on Cloud
  Run with no long-lived process.
- **Bucket-only inbox.** For tests and for a deployment with no second
  service: an unconditional zero-byte PUT at `q/<group>/<task id>`, listed by
  the group's workers and deleted once acquired. Same semantics, higher
  latency, zero dependencies beyond the bucket.

If the target is this process, the message is handed over in memory and
nothing leaves the process.

### worker.py and sdk/ — the posts, made real

`worker.py` is `execute_until_blocked_outer`: acquire, heartbeat, run the
function from the top, then `fulfill` on return, `suspend` on `Blocked`, or
`release` on an unexpected error. `sdk/` is `@resonate`, `durable()` with
positional ids from a contextvar, `.rpc` which creates the callee's promise
with a `resonate:target` tag so the kernel dispatches it, and `gather`, which
collects every `Blocked` id and suspends on all of them at once. Nothing in
`sdk/` knows about the bucket or Pub/Sub; it calls `applier.submit`.

## 3. Order of work

1. **`store.py`, `doc.py`, `kernel.py` over `MemoryStore`.** Unit tests for
   first-writer-wins, fencing, fan-out, timeouts. Every listing from post 001
   runs against this.
2. **`applier.py` and `timerd.py`.** Crash-window tests with `FaultStore`: kill
   after arm, after CAS, after disarm, after send; assert each is repaired.
3. **`sdk/` and `worker.py`.** The research agent from the repo README runs end
   to end in one process, is killed at random points, and resumes.
4. **`transport.py` and a second process.** Two workers over the bucket-only
   inbox on `MemoryStore`, then the Pub/Sub transport against the emulator.
   `rpc` crosses the process boundary.
5. **`GcsStore`.** Live test against a real bucket: `fake-gcs-server` honours
   generation preconditions well enough for CI, but the claim "it works on
   GCS" is only true once it has run against GCS. Two processes, one bucket,
   randomized traffic, and a snapshot diff against the Rust
   `resonate-server-blob` in-memory server on the same requests, so our
   semantics are held to theirs.

Line budget, first estimate: store 250, doc 300, kernel 900, applier 300,
timerd 150, transport 200, worker 200, sdk 400, roughly 2,700 for the engine
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
- **Timers on GCP.** In-process `timerd` is enough for a long-lived worker. On
  Cloud Run, a Cloud Scheduler job hitting a sweep endpoint every N seconds is
  the same loop driven from outside; the sweep is idempotent either way.
- **Search and observability.** A search reads every document. Off by default,
  as in both reference servers.
