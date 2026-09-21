# final — the engine, end to end, in Python, on S3

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

**The S3 store (`resonatehq/resonate`).** Two independent implementations of
the same design exist and agree with each other: `crates/resonate-server-blob`
(Rust, on `main`) and `impl/server/s3` (Zig, on branch
`claude/resonate-s3-zig-5kf2ia`, whose README says "whose only durable state is
objects in an S3 bucket"). The design:

- **One document per origin.** Everything before the first `:` of an id is the
  origin; `wf/<origin>` holds every promise and task of that origin. Every
  protocol operation is single-origin, so one conditional write commits a whole
  transition. No log, no lock, no lease object, no consensus.
- **CAS is what S3 gives you.** `If-None-Match: *` to create, `If-Match: <etag>`
  to replace. `412` means the state moved: re-read and **re-decide**, never
  replay. `409` means the store could not order two writes: retry the same
  write. No answer: tell the caller and let it retry, every op is idempotent.
  Real conditional writes are required (S3, R2, GCS, Azure); MinIO silently
  loses writes.
- **Deadlines are keys.** `t/<NN>/<20-digit deadline>_<origin>` as zero-byte
  objects; a capped ascending LIST returns the nearest deadlines. `NN` shards a
  monotone key space. A timer write is unconditional because the key *is* the
  record.
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
server *is* the database". We do the same with S3: the SDK talks to the bucket
directly, and the kernel runs inside the worker.

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
 Inbox     q/<group>/<task id> zero-byte keys            inbox.py    how work finds a worker
 ────────────────────────────────────────────────────────────────────────────
 Store     get / put_if_match / put_if_none_match /     store.py    MemoryStore, S3Store
           put / delete / list(prefix, max_keys)
```

### store.py — the object-store port

Six operations, an `Etag`, and a three-way error: `PreconditionFailed`,
`Conflict`, `Unavailable`. `MemoryStore` implements the same CAS semantics in a
dict and drives every test. `S3Store` wraps `aiobotocore` (or `boto3` in a
thread) and maps `412` / `409` / everything else onto the three errors. Optional
`FaultStore` that fails after N writes, to cut the power between two effects.

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

### inbox.py — how a worker finds work

S3 cannot push, so the `Send(execute)` effect for a target becomes an
unconditional zero-byte PUT at `q/<group>/<task id>` (idempotent by key, like a
timer). A worker LISTs its group prefix, calls `task.acquire` (the CAS on the
document is what decides who wins), and deletes the inbox key once acquired. A
lost delete is harmless: the next lister's acquire is refused by the version.
If the target is this process, the message is handed over in memory and no key
is written. An optional transport port (SQS, HTTP) can replace the inbox later
without touching the kernel.

### worker.py and sdk/ — the posts, made real

`worker.py` is `execute_until_blocked_outer`: acquire, heartbeat, run the
function from the top, then `fulfill` on return, `suspend` on `Blocked`, or
`release` on an unexpected error. `sdk/` is `@resonate`, `durable()` with
positional ids from a contextvar, `.rpc` which creates the callee's promise
with a `resonate:target` tag so the kernel dispatches it, and `gather`, which
collects every `Blocked` id and suspends on all of them at once. Nothing in
`sdk/` knows about S3; it calls `applier.submit`.

## 3. Order of work

1. **`store.py`, `doc.py`, `kernel.py` over `MemoryStore`.** Unit tests for
   first-writer-wins, fencing, fan-out, timeouts. Every listing from post 001
   runs against this.
2. **`applier.py` and `timerd.py`.** Crash-window tests with `FaultStore`: kill
   after arm, after CAS, after disarm, after send; assert each is repaired.
3. **`sdk/` and `worker.py`.** The research agent from the repo README runs end
   to end in one process, is killed at random points, and resumes.
4. **`inbox.py` and a second process.** Two workers, one `MemoryStore` served
   over a socket, then one real bucket. `rpc` crosses the process boundary.
5. **`S3Store`.** Live test against S3 or R2 (not MinIO). Two processes, one
   bucket, randomized traffic, and a snapshot diff against the Rust
   `resonate-server-blob` in-memory server on the same requests, so our
   semantics are held to theirs.

Line budget, first estimate: store 250, doc 300, kernel 900, applier 300,
timerd 150, inbox 100, worker 200, sdk 400, roughly 2,600 for the engine and
the rest for tests.

## 4. Decisions to make

- **Wire vocabulary.** Speak Resonate's envelope (`{"kind": "task.acquire",
  "head": {...}, "data": {...}}`) so the differential test in step 5 is free,
  or the posts' smaller vocabulary and translate only in the test. Recommended:
  Resonate's names for the kernel's `Req` types, the posts' names in `sdk/`.
- **Multiple processes on one origin.** The per-origin actor serializes writers
  inside one process; across processes the CAS does. Cache reads must be
  revalidated with `If-None-Match: <etag>` unless a `sole_writer` flag says
  otherwise.
- **Search and observability.** A search reads every document. Off by default,
  as in both reference servers.
