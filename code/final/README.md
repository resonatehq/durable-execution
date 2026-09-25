# final — the engine, end to end, in Python, on GCP

This folder is where the engine gets built for real: a durable-execution
runtime, `resonate`, that runs as one Cloud Run service over a Cloud Storage
bucket and a Cloud Tasks queue. Section 0 is what exists. Sections 1–5 are
the original plan, kept as history. Section 6 is how to deploy.

## 0. What exists

### What a user writes

```python
# main.py
from resonate import gather, resonate, serve

@resonate
async def research(question: str):
    # Plan the searches
    queries = await agent(f"Plan the searches for: {question}")

    # Fan out the searches
    results = await gather(search.rpc(q) for q in queries)

    # Synthesize the results
    return await agent(f"Write a cited report. {question}: {results}")

handler = serve()
```

Nothing in it mentions promises, tasks, leases, retries or recovery. The
public surface is `serve`, `resonate`, `gather`, `sleep`, `external`,
`Failed` and `Durable`, and that is all of `resonate/__init__.py`.
`examples/research-agent/main.py` is this program in full. `test_e2e.py`
runs the same program over a simulated bucket and cuts the power at every
write the run performs. Each time it still finishes with the same answer,
and at most the in-flight call is repeated.

Async is not a detail. `gather` has to dispatch every branch before anything
blocks, or the branches run one at a time, and in async that falls out: each
branch is a coroutine, they all run up to the point where their value is not
there yet, and only then is there anything to wait for. A leaf that only
prompts a model may be a plain `def`, because it has nothing to await and
should not have to pretend.

### The service

`handler = serve()` builds a `Server` (`resonate/server.py`) from the
environment. `resonate/config.py` holds `serve` and `build`, and it is the
only module that reads environment variables. The server has one route,
`POST /`, and the body's `kind` says what arrived:

| kind | what happens |
|---|---|
| `execute` | the queue delivers a dispatch; the worker runs the task |
| `timeout` | the queue delivers a deadline; the engine sweeps that origin |
| anything else | a protocol request (`promise.create`, `task.acquire`, ...), parsed by `parse_request` |

When `ROUTES_ACCOUNT` is set, `execute` and `timeout` must carry the queue's
OIDC token. Any other path is a 404, and any other method is a 405.

The queue carries three messages (`resonate/types.py`): `Execute(task_id,
version)`, `Unblock(promise)` and `Timeout(origin)`. They form one Pydantic
union, discriminated on `kind`. On the wire they look like
`{"kind":"execute","taskId":...,"version":...}` and
`{"kind":"timeout","origin":...}`. A deadline is a timeout message the
service sends to itself:
`queue.create(HERE, {"kind":"timeout","origin":...}, not_before=...)`,
where `HERE = "/"` and the queue resolves it against `BASE_URL`.

### Kernel and engine

The **kernel** (`resonate/kernel.py`) is the protocol's state machine as
pure functions, modeled on the Lean implementation in `resonatehq/s3`.
Each operation is `(doc, req, ...) -> (Reply, Commands)`. `Commands` holds
`add`, the new versions of the objects the operation changed, and `send`,
the messages to send. An operation never mutates the document it reads.
`sweep(doc, now, cfg)` makes four passes: it expires promises, runs their
settlement chains, re-dispatches tasks past their retry deadline, and
reclaims tasks past their lease. Commands merge in sequence, and each step
reads `c.view(doc)`. `commit(doc, c)` turns the result into effects. If
`add` is empty there are none, so a read writes nothing. Otherwise the
effects are `[SetTimeout?, SetDocument, DelTimeout?, Send...]`. Each document
has one deadline (`min_deadline`) and is written whole. The entry points are
`handle_external` (sweep, then one request) and `handle_internal` (sweep
only).

The **engine** (`resonate/engine.py`) is the only place that does I/O:

```python
Engine(store, queue, cfg, prefix)
engine.process(msg, now) -> Reply      # msg is a protocol request or a Timeout
```

It reads the document, calls the kernel, and returns early if there are no
effects (`if not fx: return reply`). Otherwise it arms, makes a conditional
put, disarms, and sends. A deadline and a dispatch are the same object in
Cloud Tasks: a task with an HTTP target and a time before which it must not
be delivered. So both go through one queue port, and only the order of the
effects keeps them apart. Four rules carry the design, and each one is a
test:

- **Arm before the commit.** A committed document whose deadline was never
  armed is the one state nothing repairs. A failed arm fails the request.
- **One conditional write.** A `Conflict` goes back to the caller. The engine
  never loops, because a loop would choose a retry policy before anything
  has said what it should be.
- **Disarm by name, after the commit.** The queue returns a name when it
  arms a deadline, and the document records it. A writer removes the timer
  its own predecessor armed, never one found by coordinates that someone
  else has since re-armed.
- **Send after the commit.** A message is always a consequence of committed
  state, never of an intention.

`test_queue.py` watches that order through the queue and the bucket
together:

```
schedule /                at 30000     the deadline, first
commit                                 then the state
schedule worker://agent   at 0         then the message
```

Only deadlines carry a time. A dispatch is never deferred, because anything
that must wait does so by having a deadline.

### Serialization

Serialization is Pydantic everywhere. Request dataclasses carry Pydantic
field metadata. `types.py` has the `wire`, `adapter` and `record` helpers,
and replies (`Promise.to_record`, `Task.to_record`) are Pydantic dumps. The
document's own three functions sit in `resonate/engine.py`, which is the
only production code that reads or writes one: `encode(doc)`/`decode(raw)`
are the document as camelCase JSON through a `TypeAdapter(Document)`, in
text rather than bytes because that is what a store takes, and `doc_key`
gives the key it lives under.

### The two ports and their contracts

The engine is written against two protocols. `StoreP` has `get`, `put`,
`delete` and `list`, and `QueueP` has `create` and `delete`. A store or
queue refuses in one of two ways (`resonate/errors.py`). `Conflict` means
the write lost a race. `Unavailable` means there was no answer.

What each interface *is* lives in `resonate/spec/`, one module per
interface. What an implementation has to *do* to be one lives next door in
`resonate/testing/conformance/`, one suite per interface:

```
spec/engine.py   EngineP  EngineC  EngineM    engine.py
spec/store.py    StoreP   StoreC   StoreM     store_mem.py   store_gcp.py
spec/queue.py    QueueP   QueueC   QueueM     queue_mem.py   queue_gcp.py
```

The split is the one the deployment already makes. `spec/` is types and
nothing else, which is what lets `engine.py` be written against `StoreP`
without importing anything from the test harness; `conformance/` is how an
implementation is checked, so it sits under `testing/` with the simulators
and nothing in production imports it.

Each has the same three layers. `…P` is the thing once it exists, `…C` is
how one is made, and `…M` is a module that offers one under an agreed name
(`Engine`, `Store`, `Queue`). A contract is handed the module. It cannot be
handed a class, because an implementation may choose its class at import
time. It cannot be handed an instance, because only the caller knows how to
configure one. Only `EngineC` fixes a signature, because every engine takes
the same two ports. `StoreC` and `QueueC` name nothing, because their
arguments are a deployment, not an interface.

A contract lives with its interface rather than beside an implementation.
A suite that shipped with the simulator would grade the bucket against a
rival instead of against a contract. The store has 11 claims and the queue
has 8. The engine is graded on a script, against the specification's
catalogue:

```python
from resonate import engine
from resonate.testing import store_mem, queue_mem
from resonate.testing.conformance import engine as engine_suite
from resonate.testing.conformance import store as store_suite
from resonate.testing.conformance import queue as queue_suite

assert engine_suite.conformance(engine) == []
assert store_suite.conformance(store_mem) == []
assert queue_suite.conformance(queue_mem) == []
```

```
python -m resonate.testing.conformance.check
```

This command walks all three interfaces. For each implementation it asks
three questions in order: does the module offer what its spec names, does
the thing have the operations, and does it behave. Anything it cannot check
without credentials is reported as a skip, not a pass.

`test_conformance.py` runs each contract three times. The first run uses
the simulated implementation. The second uses the real adapter over a
double that raises the libraries' own exceptions. The third runs against
Google Cloud Storage itself, but only when `GCS_BUCKET` names a bucket this
machine can reach.

Status: **the store has run on GCP, and so has the service.** On 2026-09-22
all eleven store claims passed against a real bucket. The write rates
measured in that run are in the `resonate/store_gcp.py` docstring. The queue
contract runs against Cloud Tasks only when `TASKS_QUEUE` names a queue. On
2026-09-24 the whole thing ran on Cloud Run over that bucket and a real
queue, including a run whose only dispatch was deleted from the queue and
which its deadline brought back. A green suite still does not imply a live
deployment — only a deployment does, which is why the runs are written down
where they happened.

### Files

| file | |
|---|---|
| `resonate/__init__.py` | the public surface: `serve`, `resonate`, `gather`, `sleep`, `external`, `Failed`, `Durable` |
| `resonate/sdk.py` | the programming model: `@resonate`, durable calls memoized by position, `.rpc`, `gather`, `sleep`, `external`, `Blocked`, versions |
| `resonate/kernel.py` | the protocol's state machine as pure functions: fifteen operations, `sweep`, `commit`, `handle_external`, `handle_internal` |
| `resonate/types.py` | the protocol: fifteen requests, the reply, the queue's messages (`Execute`, `Unblock`, `Timeout`), `parse_request`, and the Pydantic helpers |
| `resonate/engine.py` | `Engine.process(msg, now)`: load, decide, arm, commit, disarm, send; and the document's `doc_key`, `encode`, `decode` |
| `resonate/worker.py` | `Worker.run(task_id, version)` claims a task and decides what the outcome means; `_attempt` runs the function from the top |
| `resonate/server.py` | `Server`: `POST /`, dispatched on the body's `kind` |
| `resonate/config.py` | `serve()` and `build()`: the service from the environment, and every variable it reads |
| `resonate/spec/engine.py` | what an engine is: `EngineP`, `EngineC`, `EngineM` |
| `resonate/spec/store.py` | what a store is: four operations, two errors, `StoreP`, `StoreC`, `StoreM` |
| `resonate/spec/queue.py` | what a queue is: two operations, `QueueP`, `QueueC`, `QueueM` |
| `resonate/errors.py` | `Conflict` and `Unavailable` |
| `resonate/store_gcp.py` | a store in Cloud Storage: generation preconditions, the two failures mapped, measured write rates |
| `resonate/queue_gcp.py` | a queue in Cloud Tasks: the OIDC token, service-chosen names, the 30-day horizon |
| `resonate/testing/sim.py` | `Clock`, a clock a test can move, and `Runtime`, one process playing Cloud Tasks and Cloud Run |
| `resonate/testing/faults.py` | `Fault` and `Crash`: cut the power at the k-th write, across both ports |
| `resonate/testing/store_mem.py` | a store in a dict, with a power cut |
| `resonate/testing/queue_mem.py` | a queue in a dict: duplicate delivery, no order, lateness, giving up, and a power cut |
| `resonate/testing/properties.py` | the conformance catalogue from `resonatehq/resonate-specification`: 43 state and 50 transition entries, the sweeper checks, the known gaps |
| `resonate/testing/explore.py` | bounded exhaustive search: every reachable state to a depth, with the catalogue on every edge |
| `resonate/testing/conformance/engine.py` | what an engine has to do: a script, graded against the catalogue |
| `resonate/testing/conformance/store.py` | what a store has to do: 11 claims |
| `resonate/testing/conformance/queue.py` | what a queue has to do: 8 claims |
| `resonate/testing/conformance/violation.py` | what all three contracts report |
| `resonate/testing/conformance/check.py` | every interface against every implementation, in one command |
| `examples/research-agent/` | the program above as a deployable application |
| `examples/travel-agent/` | a translation of Temporal's durable-AI-agent tutorial: a conversation, tools, and a person confirming the step that spends money |
| `pyproject.toml` | the package, so a user installs `resonate` instead of copying it |
| `requirements.txt` | what the container installs |
| `ARCHITECTURE.md` | the parts, and which of them may touch the outside |
| `SEQUENCE.md` | the service as sequence diagrams |
| `live/` | scripts run against a real deployment: crash recovery on Cloud Tasks, and read/write latency on a real bucket |
| `test/test_kernel.py` | the operations, one test per branch, with the catalogue on every step |
| `test/test_properties.py` | one hand-built violator per catalogue entry, so every entry is shown falsifiable |
| `test/test_machine.py` | a Hypothesis state machine: randomized scripts with shrinking |
| `test/test_explore.py` | the exhaustive search at two profiles: broad and shallow, narrow and deep |
| `test/test_engine.py` | the document's key and JSON, the write law, the effect order, and every window the process can stop in |
| `test/test_spec.py` | our engine through the conformance suite, and two broken engines the suite has to reject |
| `test/test_store.py` | what only a simulated store has: the power cut |
| `test/test_queue.py` | the simulated queue, the agent over an unkind one, and the scheduling order |
| `test/test_e2e.py` | the research agent, run to completion and killed at each write |
| `test/test_sleep.py` | a durable sleep arms a deadline and wakes |
| `test/test_external.py` | `external`: a run waits on a promise somebody outside settles |
| `test/test_versions.py` | duplicate names are refused; versions of one function coexist |
| `test/test_types.py` | the three module specs, run past mypy |
| `test/test_check.py` | that `resonate.testing.conformance.check` sees every implementation, admits what it skipped, and can say no |
| `test/test_conformance.py` | the store and queue contracts against every implementation, plus what only an adapter can get wrong |
| `test/test_app.py` | `Server` without HTTP: status codes, the queue's messages, and the research agent through the service |
| `test/test_http.py` | the `handler` that `serve()` returns, in a real Flask app: routes, methods, auth, and the agent over HTTP |
| `test/test_userapp.py` | that a user's whole repository is `main.py` and a one-line `requirements.txt` |
| `test/test_deploy.py` | that `requirements.txt` covers what production imports, and each example declares what it imports |
| `test/test_example_agent.py` | the travel agent through a whole booking |

### Dependencies

Pydantic is the core package's one third-party dependency. `types.py` uses
it to validate requests, and `engine.py` uses it to read and write
documents. Three places need more. `server.py` needs Flask (through
`functions-framework`). `store_gcp.py` and `queue_gcp.py` need Google's
client libraries, and so does the token check in `Server.authorized`. Those
Google imports sit inside the methods that use them. `requirements-dev.txt`
lists the test tools and the GCP libraries as separate groups. The tests
live in `test/`, and `conftest.py` at the root puts the code on their path.

### Evidence

Several campaigns are opt-in, because they take minutes, not seconds:

```
DEEP=1 python -m pytest test/test_machine.py -k deep --hypothesis-show-statistics
python -m resonate.testing.explore --depth 7 --alphabet narrow
hypothesis fuzz -- -k TestKernelMachine      # needs hypofuzz; runs until stopped
```

There are four layers of evidence, and each answers something the others
cannot:

- **The unit tests** pin each operation's branches against the Rust kernel's
  own test suite, which ours was transcribed from.
- **The catalogue** runs on every step of every test. A kernel step is two
  abstract steps, the sweep and the operation. Each is checked on its own,
  and the fused result must equal their composition.
- **The exhaustive search** establishes reachability. On the broad alphabet
  it reaches tens of thousands of states by depth 4. The narrow alphabet
  goes deeper, which is where the long chains live.
- **The Hypothesis machine** goes further than any bound, and shrinks what
  it finds.

#### Steering the search

There are three mechanisms, and only one of them steers.

`event()` labels a test case, and the label shows in
`--hypothesis-show-statistics`. Every request emits one, so a campaign's
ratio of real work to refusals can be read off directly. It observes and
changes nothing.

`target()` is the signal. Hypothesis hill-climbs to maximize what it is
given. `teardown` gives it the widest the document got during the script,
under two labels: tasks in flight, and obligations registered between them.
Hypothesis allows at most one call per label per test case, so the call
cannot go inside a rule. It also needs volume to bite, so targeting earns
its keep only in the deep profile.

The rules are grouped by what they need, not by which operation they send,
and the operation is drawn inside the rule. Hypothesis samples a rule and
then filters it against its preconditions. A rule gated on a task state the
document rarely holds therefore costs a retry every time it is drawn.
Coarse groups keep most steps reaching the kernel instead of stopping at a
door.

**HypoFuzz** runs the same state machine as a coverage-guided campaign. It
uses real branch coverage instead of a metric we invented, and it runs for
as long as it is left running.

Three entries in the catalogue are adapted to our shape and marked in the
source, with the specification's own form kept beside them. Two are adapted
because we fuse the wake and its dispatch into one step. The third is
adapted because the specification samples it on scripts too short to reach
a re-suspension.

#### What the search found

The Hypothesis machine found a real divergence. The case is registering a
callback against a promise that has already settled. The Rust kernel wakes
a suspended awaiter there, following its SQL backend, where the
registration inserts a *ready callback* that a later step drains. The
coalesced machine has no later step. Waking there would be a transition out
of `suspended` that consumed no callback, which
`consistent_wake_follows_callback_consumption` forbids. The specification
does nothing in that branch (`spec/02-abstract/external.lean:78-83`), and
neither do we. Nothing is stranded: a task suspends only on promises that
are pending at the time, and a settlement drains every callback it holds.

The search also found a second divergence, which is reported here and not
fixed. The specification's `consistent_suspension_registers_callback`
demands a callback that is new in the step. But consider a task that
suspends on a promise, is halted, continued and re-acquired, and suspends
on the same promise again. It registers nothing new, because registration
is idempotent in the specification's own `taskSuspend`.

## 1. What we are building on (original plan)

*Sections 1–5 are the plan as it was written before the code existed. They
are kept as history. Their file names and mechanisms (`main.py` as the
shell, `doc.py`, `store.py`, `transport.py`, an outbox, `/execute` and
`/sweep` routes) do not describe the current code. Section 0 does.*

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

## 2. The system, end to end (original plan)

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

## 3. Where a process can stop (original plan)

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

(As built there is no outbox. The engine arms before the commit and sends
after it. The current crash table is in `resonate/engine.py`'s docstring.)

## 4. Order of work (original plan)

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
5. **GCS and real Cloud Tasks**, and one contract they share with the
   simulators. Then one service, two Cloud Run revisions, randomized
   traffic, and a snapshot diff against the Rust `resonate-server-blob`
   in-memory server on the same requests, so our semantics are held to
   theirs.

Line budget, first estimate: engine 900, doc 300, store 250, main 150,
transport 200, worker 250, sdk 400, roughly 2,450 for the engine and the
rest for tests.

## 5. Decisions still open (original plan)

- **Arm before or after commit.** See section 3. Recommended: before. (Built:
  before.)
- **Sequentiality.** None from Cloud Run. CAS decides; `409` rate is the
  signal. If it is high, Pub/Sub ordering keys on the origin serialize the
  dispatch path only, at the cost of head-of-line blocking per workflow, which
  may be worse than the retries. Measure first.
- **Wire vocabulary.** Resonate's envelope (`{"kind": "task.acquire",
  "head": {...}, "data": {...}}`) at the service, so the differential and the
  trace checker are free. The posts' names stay inside the SDK. (Built:
  `parse_request` in `resonate/types.py`.)
- **Who retries a `409`.** The SDK, with backoff, since every operation is
  idempotent and reports current state. The function never loops.
- **Authentication.** Cloud Tasks signs with an OIDC token for a service
  account; queue messages must carry it. (Built: `execute` and `timeout`,
  when `ROUTES_ACCOUNT` is set.)
- **The 30-day horizon.** Cloud Tasks will not schedule further out.
  (Built: `queue_gcp.Queue.create` clamps `not_before` to it.)

## 6. Deploying

What you write is one file.

```python
# main.py
from resonate import gather, resonate, serve

@resonate
def search(query: str):
    return index.query(query)

@resonate
async def research(question: str):
    queries = await agent(f"Plan the searches for: {question}")
    results = await gather(search.rpc(q) for q in queries)
    return await agent(f"Write a cited report. {question}: {results}")

handler = serve()
```

Beside it, a `requirements.txt` of one line: `resonate`. That is the
repository.

A durable function's name is the protocol's identifier. A promise carries
`{"f": "search"}`, and a worker looks the code up by that name, so two
functions cannot share one. `@resonate` refuses the second instead of letting
the last import win, because otherwise a dispatch created for one function
would silently run the other. Two *generations* of one function are a
different thing, and they are allowed:

```python
@resonate(version=1)
async def research(question: str):
    ...
```

Both stay deployed. A run finishes on the body it started on, and new runs
take the new one. Replay reads earlier calls back by position, so inserting
a durable call or reordering two is the kind of change that needs a
version. Changing what a call does is not. Unversioned is version 0.

`handler = serve()` is the whole of the wiring. Google's buildpack looks for
a module-level function named `handler`, and `serve()` builds the service
from the environment and returns one. It goes last, after the functions it
serves. It reads the environment when `main.py` is imported, so a missing
`BUCKET` fails the container at start, not on the first request.
`test/test_userapp.py` builds exactly this repository in a temporary
directory and drives it. The import is a package (`from resonate import
...`) so a user's own `app.py` or `engine.py` cannot shadow it.

It is one service, because Cloud Tasks is push-only: a worker is not a
loop, it is an endpoint. The service has one route, `POST /`, dispatched on
`kind` as described in section 0.

Everything a container needs comes from its environment, and
`resonate/config.py` is the one place that reads it:

```
SIMULATED        in-memory store, queue and clock instead of GCP
BUCKET           the Cloud Storage bucket holding the documents
PROJECT          \
LOCATION          > the Cloud Tasks queue both deadlines and dispatches go through
QUEUE            /
BASE_URL         where this service answers; every function runs here
                 unless ROUTES_WORKERS says otherwise
ROUTES_WORKERS   JSON {function name: worker url}, for a split deployment
ROUTES_ACCOUNT   the service account the queue signs with; execute and timeout
                 messages must carry its OIDC token. Unset turns that check off
AUDIENCE         the audience that token is checked against
RETRY_TIMEOUT    ms a claimed task may go quiet before it is offered again (default 30000)
LEASE            ms a worker holds a task (default 60000)
K_REVISION       this worker's id (set by Cloud Run)
```

You deploy an application, not this repository. Both applications under
`examples/` are ordinary user applications by the rules above.
`test_userapp.py` checks that neither reaches past the published surface.
Each one is deployed from its own directory:

```
cd examples/research-agent

PYTHONPATH=../.. SIMULATED=1 functions-framework --target=handler   # on a laptop

gcloud run deploy research-agent --source . --function handler \
  --set-env-vars BUCKET=...,PROJECT=...,LOCATION=...,QUEUE=...,BASE_URL=...
```

The name `main.py` is the buildpack's. It looks for a file by that name at
the root of what you deploy, and the build fails without one.

Only the example's own directory is uploaded, so the buildpack installs
`resonate` from its `requirements.txt` like any other dependency. Until the
package is published, that line has to say where the package really is:

```
resonate @ git+https://github.com/resonatehq/durable-execution@<sha>#subdirectory=code/final
```

That line has now been run. On 2026-09-24 `examples/research-agent/`
deployed from its own directory — buildpack, git install, Cloud Run — and
three runs went through it: a research run of six promises and nineteen
commits, a sixty-second `nap` that suspended and was woken by Cloud Tasks,
and a run whose only dispatch was deleted from the queue and which its
deadline brought back. `examples/research-agent/README.md` has the detail
and the two IAM grants it took. `examples/travel-agent/` is the same shape
and has not been deployed.

The deployment has to get two things right that no code here can
guarantee:

- **The bucket must honour generation preconditions.** GCS does, and
  `store_spec.conformance` is the check: run it against whatever you intend
  to run on before you run on it. The whole design is one conditional write
  per transition. A bucket that silently overwrites turns every concurrent
  request into lost state.
- **The queue's retry policy must be generous.** A dropped `execute` is
  recoverable, because the retry deadline was committed before the message
  left. A dropped `timeout` is the one loss nothing here repairs, because the
  deadline it carried was the only thing that was going to fire.
  `test_queue.py` shows the hole, and beside it the remedy: a periodic
  timeout per origin, on its own schedule, that does not depend on any single
  queued task. That sweep is part of the deployment, and it is not optional.
