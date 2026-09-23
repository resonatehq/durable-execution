import Kernel.Model
import Kernel.Invariants
import Kernel.Sweep
import Kernel.Operations
import Kernel.Theorems
import Kernel.Evolution
import Kernel.Fencing
import Kernel.Wakeups

/-!
# The server, verified

`kernel.py` — the decision half of the Cloud Run engine, the part that says
what every request and every deadline does to an origin's document — as Lean
4 total functions (`Kernel.Model`), and what is proved about it.

| theorem | file | says |
|---|---|---|
| `reachable_inv` | `Theorems` | every document the server can commit satisfies every clause of `check_invariants` |
| `handleExternal_shape`, `handleInternal_shape` | `Theorems` | effects come out arm, commit, disarm, send: one commit, the timer armed for the deadline the commit records |
| `reachable_evolves` | `Evolution` | along any run: a settled promise keeps its settlement, a version never goes down, a fulfilled task stays fulfilled, nothing is deleted |
| `stale_is_refused` | `Fencing` | once a task is past version `v`, a request carrying `v` is refused and commits exactly what a bare sweep would |
| `reachable_noLost` | `Wakeups` | every suspended task is registered on a pending promise: no wakeup is lost |

`test/test_lean.py` is what makes these statements about `kernel.py` rather
than about a Lean program that resembles it: it runs both on the same random
scripts and requires every reply, effect and committed document to be equal.
-/
