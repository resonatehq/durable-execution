# lean — the server's kernel, verified

The Cloud Run function in `code/final` is a shell around one pure function:
`kernel.py`, which decides what every protocol request and every deadline
does to an origin's document. Everything the server *guarantees* is decided
there; the shell only performs the effects it is handed, in the order it is
handed them. This folder is that kernel in Lean 4 and proofs of what it
guarantees.

```
lake build                                 # the model, and every proof checked
python -m pytest test/test_lean.py         # from code/final: the model against kernel.py
```

Lean 4.33, no Mathlib. `lean-toolchain` pins the version for `elan`.

## What is proved

Each theorem quantifies over every document the server can ever commit:
`Reachable cfg d` is anything reachable from the empty document by any
sequence of requests and deadlines, at any times, under any configuration.

| theorem | says |
|---|---|
| `reachable_inv` | Every clause of `check_invariants` holds: unique ids in Dewey order; `settledAt` exactly when settled; unique listeners; each task holds the one timer its state calls for; callbacks and resumes name objects that exist; `timer_at` is the earliest armed deadline. Also three clauses the Python does not check: a pending promise's task is not fulfilled, a settled one's is, and an object has a task exactly when its promise has a target. |
| `handleExternal_shape`, `handleInternal_shape` | Effects come out as arm, then commit, then disarm, then send. There is exactly one commit, the armed timer is the deadline the commit records, and the disarmed one is the timer that was armed before. The crash story in `README.md` §3 rests on this order. |
| `reachable_evolves` | Along any run: once a promise settles, its state, value and `settledAt` never change (first writer wins). Id, tags, param and deadline never change. Objects are never deleted. A task's version never goes down, and a fulfilled task stays fulfilled. |
| `stale_is_refused` | Once a task is past version `v`, any `task.acquire`, `release`, `fulfill`, `suspend` or `fence` carrying `v` is refused with a 4xx. The step it arrives in commits exactly what a bare sweep at that instant would. Fencing works. |
| `reachable_noLost` | Every suspended task is registered as a callback on a promise that is still pending. The only thing that ever wakes a suspended task is a settlement chain on a promise it is registered on, so this says no wakeup is lost. `kernel.py` argues it in prose to justify departing from the Rust kernel in `promise_register_callback`; here it is a theorem. |
| `pending_workflow_has_timer` | While a workflow's root promise is pending, the document has a timer armed, firing no later than the root's deadline. It is the earliest deadline anywhere in the workflow, not necessarily the root's. `reachable_timer` says the same of every pending external promise. Stating this found a bug, since fixed: the kernel armed only *targeted* promises, so a durable sleep (an external timer with no target) was never armed. A one-minute sleep in a one-day workflow slept a day, then timed out with its root. The theorem is about the document's `timerAt`; whether the queue really holds that task is the shell's job (see the arm-before-commit race). |

`#print axioms` on each shows only `propext`, `Classical.choice` and
`Quot.sound`. There is no `sorry`, `admit` or `native_decide`.

## Why these are statements about `kernel.py`

A proof about a Lean program is a proof about `kernel.py` only as far as the
two are the same function. Nothing verifies Python directly, so the bridge is
checked from both ends:

- **The transcription is literal.** `Kernel/Model.lean` has one function per
  Python function, the same names, and the same order of checks. It is kept
  executable (no `Prop`s, no axioms), so it can be run.
- **The differential runs both.** `Main.lean` replays a script of requests
  through the Lean kernel. `test/test_lean.py` sends the same scripts through
  `kernel.py` and requires everything to be equal: every reply status and
  body, every effect in order, and every field of every committed document.
  The scripts are random walks over the two `explore.py` alphabets. A
  quarter of the requests are drawn from requests that must be refused:
  stale versions, missing ids, malformed tags and bad addresses. 600 scripts
  run by default; 88,000 steps were compared while this was written, reaching
  all 15 operations and every reply status they return.
- **The differential can fail.** `test_the_differential_catches_a_changed_kernel`
  swaps a mutant into `kernel.py`: a release that forgets its dispatch, and a
  settlement that forgets to drop its listeners. The test requires the
  differential to notice.

The model leaves out only what the kernel never reads: the document's
`clock`, `gen` and `timer_name`, which belong to the shell.

## What is not proved

- **The shell.** `engine.py`'s I/O, the store's conditional write, and the
  queue's delivery are outside the model. `handleExternal_shape` proves the
  effect order the engine relies on. That the engine performs effects in
  that order is `spec/engine.py`'s conformance suite, not a theorem.
- **Liveness.** `reachable_noLost` says a suspended task is always
  *registered* to be woken, not that it will be. That also needs the promise
  to settle and the queue to deliver.
- **The differential is sampled, not exhaustive.** It is strong evidence the
  two kernels agree, not a proof. In particular it never feeds inputs the
  Python rejects by raising: a settle state outside the five names, or an
  address `urlsplit` refuses (a bracketed host). The model returns a value
  for those; the Python throws.
- **The wire.** `wire.py`'s decoding of the JSON envelope is not modelled.

## What stating it found

Writing the invariants down found three bugs. The first is in the kernel:
it armed a deadline only for promises with a target, so a durable sleep,
an external timer promise with no target, never woke on time. Every pending
external promise now arms its deadline (`timeout_armed`), with a regression
test in `test/test_kernel.py`. The other two were in the Python checker,
not the kernel. Both are fixed in
`kernel.py`, each with a regression test in `test/test_kernel.py`:

- The sort check compared against `sorted()` of a *set*. Distinct ids can
  share a Dewey key (`o:1` and `o:01`), and sorting a set puts those in hash
  order, so a correct document reached by two ordinary `promise.create`
  requests was reported unsorted under some `PYTHONHASHSEED` values.
- `(t.retry_at or t.lease_at) is not None` treats a timer at instant 0 as no
  timer, because 0 is falsy.

Apart from the sleep bug, the kernel held up: every other theorem went
through without changing a line of it.

## Layout

| file | |
|---|---|
| `Kernel/Model.lean` | the transcription |
| `Kernel/Invariants.lean` | the invariants as propositions; Dewey order is a strict total order; lemmas about `get`, `modify`, `insert` |
| `Kernel/Sweep.lean` | the sweep preserves the invariants, through the one moment phase 1 breaks them |
| `Kernel/Operations.lean` | each of the fifteen operations preserves them |
| `Kernel/Theorems.lean` | `reachable_inv`, the effect order |
| `Kernel/Evolution.lean` | what no step can undo |
| `Kernel/Fencing.lean` | stale tokens are refused |
| `Kernel/Wakeups.lean` | no lost wakeups |
| `Kernel/Timers.lean` | every pending workflow has a timer |
| `Main.lean` | the differential driver: JSON lines in, JSON lines out |
