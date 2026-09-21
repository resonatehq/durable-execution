# final — the engine, end to end, in Python, on GCP

This folder is where the engine gets built for real. This document is the plan:
what we learned from the two posts, from `resonatehq/resonate`, and from the
design chat, and how the pieces fit end to end. Every non-obvious decision
below should grow an entry in `notes/` as it gets implemented.

## 0. What exists

The whole thing, end to end, over in-memory ports. What is left is the
outside: the GCS and Cloud Tasks implementations of three ports that already
have in-memory twins, and the HTTP routes that carry a message between two
processes rather than two objects.

The program the tests run is the one from this repository's README:

```python
@resonate
async def research(question: str):
    # Plan the searches
    queries = await agent(f"Plan the searches for: {question}")

    # Fan out the searches
    results = await gather(search.rpc(q) for q in queries)

    # Synthesize the results
    return await agent(f"Write a cited report. {question}: {results}")
```

Character for character, which is the point. Nothing in it mentions
promises, tasks, leases, retries or recovery. It runs to completion over a
simulated bucket, calls the model twice and the search index three times and
never more, and when the power is cut at any of the 25 writes the run
performs it finishes anyway with the same answer.

Async is not a detail. `gather` has to dispatch every branch before anything
blocks, or the branches run one at a time, and in async that falls out: each
branch is a coroutine, they all run up to the point where their value is not
there yet, and only then is there anything to wait for. A leaf that only
prompts a model may be a plain `def`, because it has nothing to await and
should not have to pretend.

| file | |
|---|---|
| `kernel.py` | `handle_external(doc, req, now, cfg)` and `handle_internal(doc, now, cfg)`: the protocol's state machine as a pure function, all fifteen operations |
| `engine.py` | `Engine.process(msg, now)`: load, decide, arm, commit, disarm, send. The only method, and the only place that does I/O |
| `spec.py` | what an engine is, as three protocols, and what it must do, as a conformance suite any implementation can be run through |
| `codec.py` | the document's canonical byte form, and the key it lives under |
| `ports.py` | the three things the engine needs from the world — a store, timers, a transport — with in-memory twins and a fault injector |
| `blob.py` | the bucket, as the four operations a real one offers, with a simulated bucket and the adapter that narrows it to the engine's store |
| `line.schema.json` | what a line of a document may be. An oracle, maintained by hand against the protocol, never edited to make a test pass |
| `sdk.py` | the programming model: `@resonate`, durable calls memoized by position, `.rpc`, `gather`, `Blocked` |
| `runtime.py` | a worker, which is post 002's outer half in the protocol's words, and the loop that carries messages and fires deadlines |
| `properties.py` | the conformance catalogue from `resonatehq/resonate-specification`, 43 state and 50 transition entries, the two sweeper checks, the three known gaps |
| `explore.py` | bounded exhaustive search: every reachable state to a depth, with the catalogue on every edge |
| `test_kernel.py` | the operations, one test per branch, plus the remote call from post 002 end to end |
| `test_properties.py` | one hand-built violator per catalogue entry, so every entry is shown falsifiable |
| `test_machine.py` | a Hypothesis state machine: randomized scripts with shrinking |
| `test_explore.py` | the search at two profiles, broad and shallow, narrow and deep |
| `test_engine.py` | the codec, the write law, the effect order, and every window the process can stop in |
| `test_spec.py` | our engine run through the conformance suite, over a dict and over a simulated bucket, and two broken engines the suite has to reject |
| `test_blob.py` | the bucket's four operations, and the adapter |
| `test_schema.py` | every reachable document against the schema, and 29 ways an encoder goes wrong that it has to reject |
| `test_e2e.py` | the research agent, run to completion and killed at each of its 25 writes |

The kernel has no dependencies. The tests need `pytest` and `hypothesis`
(`requirements-dev.txt`); `python -m pytest` runs in about 50 seconds.

Two campaigns are opt-in because they take minutes rather than seconds:

```
DEEP=1 python -m pytest test_machine.py -k deep --hypothesis-show-statistics
python explore.py --depth 7 --alphabet narrow
hypothesis fuzz -- -k TestKernelMachine      # needs hypofuzz; runs until stopped
```

Four layers of evidence, each answering something the others cannot:

- **The unit tests** pin each operation's branches against the Rust kernel's
  own test suite, which we transcribed from.
- **The catalogue** runs on every step of every test. A kernel step is two
  abstract steps, the sweep and the operation, so each is checked on its own
  and the fused result is held equal to their composition.
- **The exhaustive search** proves reachability. 70 869 states and 270 994
  edges at depth 4 on the broad alphabet; 11 579 states at depth 5 on the
  narrow one, which is where the long chains live.
- **The Hypothesis machine** goes further than any bound, and shrinks what it
  finds.

### Steering the search

Three mechanisms, and only one of them steers.

`event()` labels a test case and shows in `--hypothesis-show-statistics`.
Every request emits one, which is how the ratio of real work to refusals is
read off a campaign. It is observational and changes nothing.

`target()` is the signal. Hypothesis hill-climbs to maximize what it is
given, so `teardown` hands it the widest the document got during the script,
under two labels: tasks in flight, and obligations registered between them.
A document can be wide in either way independently. Two constraints shape
where the call goes: at most one per label per test case, so it cannot live
inside a rule; and it needs volume to bite, noticeably above a thousand test
cases and obviously around ten thousand per label. The default campaign runs
four hundred, so targeting earns its keep only in the deep profile, which
runs ten thousand and takes about nine minutes.

The rules are grouped by what they need rather than by which operation they
send, with the operation drawn inside. Hypothesis samples a rule and then
filters it against its preconditions, so a rule gated on a task state the
document rarely holds costs a retry every time it is drawn. Collapsing
twenty such rules into five coarse groups moved the share of steps reaching
the kernel rather than a door from 45% to 64% on the same budget, and
brought the wake and the halted awaiter's buffered resume into every
campaign instead of the lucky ones.

Beyond all of this is **HypoFuzz**, which runs the same state machine as a
coverage-guided campaign using real branch coverage rather than a metric we
invented, for as long as it is left running. It needs no change to the
tests. A short run found nothing, which is worth exactly what a short fuzz
run is worth.

Three entries in the catalogue are adapted to our shape and marked in the
source, with the specification's own form kept beside them: two because we
fuse the wake and its dispatch into one step, one because the specification
samples it on scripts too short to reach a re-suspension.

### The engine

One method, because there is one thing to do. A protocol request and a
deadline coming due differ in which kernel function decides them and in
nothing else, so `process` takes either and the caller never has to know
which shell it is talking to.

```python
def process(self, msg: Req | Timeout, now: int) -> Reply:
    origin = origin_of_msg(msg)
    raw, generation = self.store.load(doc_key(origin))
    doc = decode(raw, origin) if raw is not None else Document()
    now = max(now, doc.clock)
    fx, reply = (handle_internal(doc, now, cfg), Reply.ok({})) if isinstance(msg, Timeout) \
        else handle_external(doc, msg, now, cfg)
    ...  # write law, then: arm, commit, disarm, send
```

Four rules carry the whole design, and each one is a test:

- **Arm before the commit.** A committed document whose deadline was never
  armed is the one state nothing repairs. A failed arm fails the request.
- **One conditional write.** A `Conflict` goes back to the caller. The engine
  never loops: a loop would choose a retry policy before anything has said
  what it should be.
- **Disarm by name, after the commit.** The name comes back from whatever
  armed the deadline and is recorded in the document, so a writer removes
  what its own predecessor wrote rather than a deadline by coordinates that
  someone else has since re-armed.
- **The write law.** If the objects and the armed deadline are untouched,
  nothing is written. The clock is outside that comparison, or every read
  would be a write.

`test_engine.py` cuts the power at each of the ten writes the exercise
performs — across the store, the timers and the transport — then does what
the world does, retrying the request and firing the deadlines, and requires
the run to reach the promises and tasks a clean run reached. It also covers
the window nothing can close over a network: a commit that landed and whose
answer was lost.

### The specification of an engine

`spec.py` names three things, because three things need naming and they are
not the same:

```python
class EngineP(Protocol):        # an engine, once it exists
    def process(self, msg: Msg, now: int) -> Reply: ...

class EngineC(Protocol):        # how one is made
    def __call__(self, store: Store, timers: Timers, transport: Transport,
                 cfg: KernelCfg = ..., prefix: str = ...) -> EngineP: ...

class EngineM(Protocol):        # a module that offers one
    Engine: EngineC
```

The module is the useful layer. A conformance suite cannot be handed a
class, because an implementation may want to choose its class at import
time, and it cannot be handed an instance, because the suite has to supply
the world the engine runs in. It is handed the module and reaches for
`Engine`. The ports are constructor arguments for the same reason: that seam
is what lets one engine run over a bucket in production and over a dict in a
simulation, which is what makes a simulated run a real run.

`EngineP` has one member and no name, no identity and no lifecycle.
Everything an engine knows is in the bucket, so two of them are
interchangeable.

The types say nothing about behaviour. `conformance(module)` is the part
that does: it drives an engine through a standard script and returns
everything it broke. Four checks, independent of each other:

- every document committed is a state the catalogue admits, and every
  consecutive pair a transition it admits;
- a `Timeout` step is held additionally to the sweeper properties, which are
  strictly stronger than the general edge tables;
- the effect order, arm then commit then disarm then send, because that is
  what the crash windows rest on;
- the write law, restated in `spec.py` rather than imported from the engine,
  since a specification that borrowed the implementation's comparison would
  only be checking that the implementation agrees with itself.

Two deliberately broken engines are in `test_spec.py`, one that writes on a
read and one that sends before it commits, because a conformance suite
nothing has ever failed proves as little as one nothing has ever passed.

### What the search found

The Hypothesis machine found a real divergence. Registering a callback
against a promise that has already settled: the Rust kernel wakes a suspended
awaiter, following its SQL backend, where the registration inserts a *ready
callback* that a later step drains. The coalesced machine has no later step,
so waking there is a transition out of `suspended` that consumed no callback,
which `consistent_wake_follows_callback_consumption` forbids. The
specification does nothing in that branch
(`spec/02-abstract/external.lean:78-83`), and neither do we now. Nothing is
stranded by the change: a task suspends only on promises that are pending at
the time, and a settlement drains every callback it holds, so a suspended
task always has a rung on a pending promise.

A second finding, reported here rather than fixed: the specification's
`consistent_suspension_registers_callback` demands a callback that is new in
the step, but a task that suspends, is halted, continued, re-acquired and
suspends on the same promise again registers nothing new, because
registration is idempotent in the specification's own `taskSuspend`. Its
corpus is scripts of length three, which cannot reach that path.

## 1. What we are building on

**The programming model (posts 001 and 002 in `design/content/writing`).**
One primitive, the durable promise, with two operations, `create` and `settle`,
both first-writer-wins and therefore idempotent. Calls are memoized by
*position* (`run:1`, `run:2`, `run:2:1`), so replay derives the same id every
time. `durable(store, id, func, *args)` is the runtime frame under every call.
A remote call is the same promise with `create` here and `settle` over there;
the frame raises `Blocked(id)` to unwind the stack, the outer loop subscribes
the task to the promise and releases it, and a settle later re-queues the
task, which is re-run from the top.

**The engine shape (`resonatehq/resonate`).** Two independent servers over
an object store exist and agree with each other: `crates/resonate-server-blob`
(Rust, `main`) and `impl/server/s3` (Zig, branch `claude/resonate-s3-zig-5kf2ia`).
Both rest on one property: every protocol operation is single-origin, so **one
document per origin** and **one conditional write** commit any transition. The
state machine is a pure function, `handle(doc, req, now) -> (effects, reply)`,
and the shell performs the effects. The Lean specification on the same branch
(`spec/`, with `lake exe checktrace`) is the source the engine is transcribed
from, and the trace checker is how a transcription error is caught.

**The deployment (the design chat).** The server is a **Cloud Run function**:
a Functions Framework HTTP handler is the shell, `Engine.process` is pure, and
the request/response cycle is the only time the shell runs. Three consequences
of the host, none touching the engine: no in-process timer wheel, no CPU after
the response returns, and no instance affinity. Decisions the chat settled:

- The handler is straight-line. A CAS conflict is a `409` to the caller; retry
  policy stays out of the shell.
- Effects partition into **writes** (frontier-local: settle, register, resume)
  and the **outbox** (dispatch, notify, arm timer, disarm timer). The outbox
  commits in the same CAS as the state. `drain` after commit is best-effort;
  the outbox is the guarantee.
- Three message kinds: `invoke` and `resume` carry a task and are recoverable
  from the store if lost; `notify` carries no task and has no backstop, so its
  durability requirement is the strictest. Every entry carries an idempotency
  key `(promise id, kind, counter)`.
- Routing is advisory, CAS is authoritative. Contention on a hot origin shows
  up as retry rate, and that rate is measured before any ordering machinery is
  added.

**What GCP supplies.** GCS has real conditional writes via generation numbers.
Cloud Tasks has `schedule_time`, which Pub/Sub lacks, so Cloud Tasks carries
both the outbox and the deadlines. Cloud Tasks makes no ordering promise, which
is fine because ordering was never the correctness gate.

## 2. The system, end to end

```
 SDK worker (any process)          ── HTTP POST / ──▶   Cloud Run function      main.py
   @resonate, durable(), .rpc,                            parse → load → process → CAS → drain → reply
   execute_until_blocked_outer                                │       │        │        │
   /execute, /sweep routes                                    │       │        │        └─ Cloud Tasks create
                                                              │       │        └─ GcsStore.commit(if_generation)
        ▲          ▲                                          │       └─ Engine.process   (pure)   engine.py
        │          │                                          └─ GcsStore.load            (GCS)    store.py
   invoke/resume   notify ──▶ subscriber URL
        │          │
   Cloud Tasks queues: `execute` (invoke, resume, notify)  and  `timers` (sweep origin at deadline → POST /sweep)
```

### main.py — the shell

```python
STORE = GcsStore.from_env()      # module scope: one per instance
ENGINE = Engine()                # no state of its own

@functions_framework.http
def handler(request):
    try:
        req = Request.parse(request.get_json(force=True))
    except (ValueError, TypeError) as e:
        return {"error": str(e)}, 400

    frontier, generation = STORE.load(req.origin)
    resp, effects = ENGINE.process(frontier, req, now=clock())
    writes, outbound = partition(effects)

    try:
        STORE.commit(req.origin, writes, outbox=outbound, if_generation=generation)
    except Conflict:
        return {"error": "conflict"}, 409

    drain(req.origin, outbound)   # best effort, before returning: no CPU after
    return resp, 200
```

A second route, `POST /sweep/<origin>`, loads the frontier, runs
`ENGINE.drain(frontier, now)` for every deadline that has passed, commits the
same way, and re-drains whatever is still in the outbox. It is the only other
entry point.

### engine.py — pure

`Engine.process(frontier, req, now) -> (reply, [Effect])` over the request
surface the spec fixes: `promise.create`, `promise.settle`, `promise.get`,
`promise.register_callback`, `task.create`, `task.acquire`, `task.heartbeat`,
`task.suspend`, `task.fulfill`, `task.release`. `Engine.drain(frontier, now)
-> [Effect]` settles expired promises, re-dispatches tasks past `retry_at`,
and reclaims tasks past `lease_at`. No clock, no ids, no I/O: `now` and every
id arrive in the request. The effect enum is closed, so `partition` matches
exhaustively:

```python
Effect = Settle | Register | Resume            # writes: stay in the frontier
       | Dispatch | Notify | ArmTimer | DisarmTimer   # outbox: leave it
```

`Resume` is a write, not a message: the frontier is closed under await, so
every awaiter of a settling promise is in the document just loaded. The
*message* half of a resume is a `Dispatch` for the awaiting task.

### doc.py — the frontier

One origin's promises, tasks, armed deadline, and outbox, with a canonical
encoding (one JSON line per entity, fixed key order, omit-empty) so state-equal
means byte-equal and the write law holds: a decision that changed nothing
writes nothing. The outbox is a list of `Message` entries:

```python
@dataclass(frozen=True)
class Message:
    kind: Literal["invoke", "resume", "notify"]
    target: str            # from the promise's target tag, or the subscriber address
    promise_id: str
    task: Task | None      # None iff kind == "notify"
    not_before: int | None # resonate:delay, durable sleep, retry backoff
    idem: str              # (promise_id, kind, counter)
```

### store.py — GCS

`load(origin) -> (Frontier, generation)` and `commit(origin, writes, outbox,
if_generation)`, one object per origin at `wf/<origin>`. Create with
`if_generation_match=0`, replace with `if_generation_match=<generation>`. A
`412` is `Conflict`. A `429`/`503` is `Unavailable`, and the caller retries.
GCS allows roughly one write per second per object, and a function has no
per-origin actor to batch behind, so a hot origin surfaces as `409`s. That is
the retry rate the chat says to measure first. `MemoryStore` has the same CAS
semantics over a dict and drives every test.

### transport.py — drain, as Cloud Tasks

`drain(origin, outbound)` turns each outbox entry into one Cloud Tasks create:

| entry | queue | HTTP target | schedule_time |
|---|---|---|---|
| `invoke`, `resume` | `execute` | the group's `POST /execute`, body `{task id, version}` | `not_before` if any |
| `notify` | `execute` | the subscriber's address, body `{promise id}` | none |
| `ArmTimer` | `timers` | the function's `POST /sweep/<origin>` | the deadline |
| `DisarmTimer` | | delete by the task name the document recorded | |

The task name is Cloud Tasks' own: a caller-chosen name leaves a tombstone
after deletion, so re-arming the same `(origin, deadline)` within the hour
would be refused. The document records the returned name next to `timer_at`.
Messages carry ids, never values: a receiver reads the frontier, so a
delivery that outruns or outlives its commit is a harmless "go look".

Delivery is at-least-once. `invoke` and `resume` are safe under it because
the handler's `task.acquire` is fenced by version and a refused acquire still
returns `2xx`. `notify` is safe because the subscriber dedupes on `idem`.

### worker.py and sdk/ — the posts, made real

A worker is an SDK process with two HTTP routes. `/execute` is
`execute_until_blocked_outer`: acquire, heartbeat, run the function from the
top, then `fulfill` on return, `suspend` on `Blocked`, or `release` on an
unexpected error. `/notify` hands a settled external promise to whatever
subscribed. `sdk/` is `@resonate`, `durable()` with positional ids from a
contextvar, `.rpc`, which creates the callee's promise with a target tag so
the engine emits a `Dispatch`, and `gather`, which collects every `Blocked`
id and suspends on all of them at once. Nothing in `sdk/` knows about GCS or
Cloud Tasks; it speaks the protocol to the function's URL and retries a `409`.

## 3. Where a process can stop

| stopped after | what is left | what repairs it |
|---|---|---|
| load or process | nothing | the caller retries |
| commit | state and outbox durable, nothing drained | the next request on the origin, or the sweep, re-drains |
| a partial drain | some Cloud Tasks created, some entries still marked undrained | re-drain; every entry is idempotent |
| drain, before the reply | everything delivered, caller told nothing | the caller retries; every operation reports current state |

One row needs a decision. "The sweep re-drains" only holds if a sweep is
scheduled, and the sweep is itself an `ArmTimer` entry in the outbox. The
reference servers close this by arming the deadline **before** the CAS, so a
committed document never lacks its timer. Recommended: keep `ArmTimer` in the
outbox for the partition's sake, but perform it first, before the commit, and
have `min_deadline` count a non-empty outbox as a deadline of `now +
redrain_after`. Then every undrained outbox has a timer, an orphan timer from
a failed commit fires into a no-op, and `notify` gets the backstop the chat
says it lacks.

## 4. Order of work

1. **`engine.py`, `doc.py`, `store.py` over `MemoryStore`.** Unit tests for
   first-writer-wins, fencing, fan-out, timeouts, the effect partition. Every
   listing from post 001 runs against this.
2. **`main.py` and `transport.py` in memory.** The handler under
   `functions-framework --target=handler` locally, with an in-process stand-in
   for Cloud Tasks (an HTTP client with a delay heap). Crash-window tests with
   a `FaultStore`: stop after commit, after a partial drain, after drain.
3. **`sdk/` and `worker.py`.** The research agent from the repo README runs end
   to end against the local function, is killed at random points, and resumes.
4. **Two workers.** `rpc` and durable sleep cross the process boundary, still
   on the stand-in. The Lean trace checker runs over the recorded requests.
5. **GCS and real Cloud Tasks.** `fake-gcs-server` honours generation
   preconditions for CI, but "it works on GCS" is only true once it has run on
   GCS. One function, two Cloud Run workers, two queues, randomized traffic,
   and a snapshot diff against the Rust `resonate-server-blob` in-memory
   server on the same requests, so our semantics are held to theirs.

Line budget, first estimate: engine 900, doc 300, store 250, main 150,
transport 200, worker 250, sdk 400, roughly 2,450 for the engine and the
rest for tests.

## 5. Decisions still open

- **Arm before or after commit.** See section 3. Recommended: before.
- **Sequentiality.** None from Cloud Run. CAS decides; `409` rate is the
  signal. If it is high, Pub/Sub ordering keys on the origin serialize the
  dispatch path only, at the cost of head-of-line blocking per workflow, which
  may be worse than the retries. Measure first.
- **Wire vocabulary.** Speak Resonate's envelope (`{"kind": "task.acquire",
  "head": {...}, "data": {...}}`) so the differential and the trace checker are
  free. Recommended: yes, at the function; the posts' names inside `sdk/`.
- **Who retries a `409`.** The SDK, with backoff, since every operation is
  idempotent and reports current state. The function never loops.
- **Authentication.** Cloud Tasks signs with an OIDC token for a service
  account; the worker and the sweep route verify it and accept nothing else.
- **The 30-day clamp.** Cloud Tasks schedules at most 30 days out. `ArmTimer`
  clamps to `min(at, now + 30d)` and a no-op sweep re-arms. It gets its own
  test, because a bug there makes a promise never time out.
