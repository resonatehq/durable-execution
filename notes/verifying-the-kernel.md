# Verifying the kernel: a transcription, and a differential to hold it honest

`code/final/lean` proves things about the server, but it cannot prove them
about Python: nothing does. So what it proves is about a Lean transcription
of `kernel.py`, and a test compares the transcription with the original. This
note says why that shape and not another.

## What was verified, and why there

The server is a shell around `kernel.py`: load, decide, arm, commit, disarm,
send. Only the "decide" step makes choices; the shell does what it is told,
in the order it is told. So what the server guarantees is what the kernel
guarantees, plus the shell keeping to the order, and the order is itself a
property of the kernel's output (`handleExternal_shape`). Verifying the
kernel verifies the part that can be wrong in interesting ways.

## Alternatives

**Verify the Python.** There is no tool for it that would survive contact
with `copy.deepcopy`, dataclasses and `match`. Rejected: not available.

**Write the kernel in Lean and generate the Python.** The proofs would then
be about the code that runs. But the kernel is written to be read, and
generated code is not. The repository's premise is code written to be read.
Rejected.

**Prove things about the specification's abstract machine**
(`resonate-specification`). That machine is not our kernel. We fuse two of
its steps into one, and `properties.py` records three places where the two
differ. A proof about it says nothing about our fusion. Rejected. Its
catalogue already runs against our kernel on every test step, which is the
right use for it.

**More search instead of proof.** `explore.py` and the Hypothesis machine
already search. Search bounds depth, and the property we most wanted — no
lost wakeup — is about arbitrarily long histories: a registration made long
ago, then a halt, a continue, and a re-suspension. Kept, and complemented.

## The shape that won

1. `Kernel/Model.lean` transcribes `kernel.py` function for function, in
   total functions, executable.
2. The theorems quantify over every reachable document, not over scripts.
3. `test/test_lean.py` runs both kernels over the same random walks and
   requires byte-equal replies, effects and documents. It is required to
   catch two planted mutants, so a differential that has gone blind fails
   the suite too.

The weak link is step 3: it samples. The mitigation is that the walks come
from the same generators that already drive the kernel into its long chains,
plus a stream of requests built to be refused.

## Two things the proof shape taught

- **A settlement is two writes, and between them the document is broken.**
  The sweep settles every expiring promise first and fulfils their tasks
  afterwards, one at a time. Rather than restructure the kernel, the proof
  carries the set of objects allowed to be broken (`Held E`) and shows each
  settlement chain takes exactly one object out of it. The same device,
  `Rungs E`, carries no-lost-wakeups through the sweep.
- **Freshness is what makes a settlement wake rather than skip.** A chain
  skips an awaiter that is past its own deadline. After the sweep's first
  phase no pending promise is past its deadline (`Fresh`), so within a step
  the skip only ever fires for awaiters that are themselves being fulfilled.
  That is why the sweep runs before every request, and the proof says so.
