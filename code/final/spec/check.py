"""Is every interface implemented, and does every implementation hold?

    python -m spec.check

Three interfaces, top to bottom, and for each one every implementation of
it. Three questions per implementation, in order, because each only makes
sense if the one before it passed:

1. **Does the module offer what its spec names?** `StoreM` says a store
   module has `Store`; this asks whether it does. A type checker asks the
   same question better (`test_types.py` runs mypy over the module specs),
   but it asks at a moment nobody is watching.
2. **Does the thing have the operations?** Every member of `StoreP` present
   on the class. Structural, so it catches a rename and nothing subtler.
3. **Does it behave?** The contract from the spec module — 11 claims for a
   store, 8 for a queue, and for the engine a script graded against the
   specification's own 93-entry catalogue.

What cannot be answered without credentials says so rather than passing.
An adapter's shape is checked here; its behaviour is checked by
`test_conformance.py`, against a double always and against Google when
`GCS_BUCKET` names a bucket and `TASKS_QUEUE` names a queue.
"""

from __future__ import annotations

import os
import sys

import engine
import queue_gcp
import queue_mem
import store_gcp
import store_mem
from spec import engine as engine_spec
from spec import queue as queue_spec
from spec import store as store_spec

OK, BAD, SKIP = "ok", "FAIL", "skip"
failures: list[str] = []


def members(protocol) -> list[str]:
    """What a protocol declares, in the order a reader would list them."""
    return [n for n in vars(protocol) if not n.startswith("_")]


def say(what: str, verdict: str, detail: str = "") -> None:
    if verdict == BAD:
        failures.append(f"{what}: {detail}")
    print(f"    {what:<34}{verdict:<6}{detail}")


def offers(module, module_protocol) -> object | None:
    """(1) The module has what its spec names."""
    wanted = members(module_protocol)
    missing = [n for n in wanted if not hasattr(module, n)]
    name = f"{module_protocol.__name__}: {' '.join(wanted)}"
    if missing:
        say(name, BAD, f"missing {', '.join(missing)}")
        return None
    say(name, OK)
    return getattr(module, wanted[0])


def implements(cls, protocol) -> bool:
    """(2) The thing has the operations."""
    wanted = members(protocol)
    missing = [n for n in wanted if not hasattr(cls, n)]
    name = f"{protocol.__name__}: {' '.join(wanted)}"
    if missing:
        say(name, BAD, f"missing {', '.join(missing)}")
        return False
    say(name, OK)
    return True


def holds(label: str, violations, detail: str = "") -> None:
    """(3) It behaves."""
    if violations:
        say(label, BAD, f"{len(violations)} violations: {violations[0]}")
    else:
        say(label, OK, detail)


# ---------------------------------------------------------------------------

print("\nengine — spec/engine.py")

print("  engine")
cls = offers(engine, engine_spec.EngineM)
if cls is not None and implements(cls, engine_spec.EngineP):
    holds("conformance", engine_spec.conformance(engine),
          f"{len(engine_spec.STANDARD_SCRIPT)} steps, every state and "
          f"transition against the catalogue")

# ---------------------------------------------------------------------------

print("\nstore — spec/store.py")

print("  store_mem")
cls = offers(store_mem, store_spec.StoreM)
if cls is not None and implements(cls, store_spec.StoreP):
    holds("conformance", store_spec.conformance(store_mem),
          f"{len(store_spec.CLAIMS)} claims")

print("  store_gcp")
cls = offers(store_gcp, store_spec.StoreM)
if cls is not None and implements(cls, store_spec.StoreP):
    bucket = os.environ.get("GCS_BUCKET")
    if bucket is None:
        say("conformance", SKIP, "set GCS_BUCKET to run it against a real bucket")
    else:  # pragma: no cover - only with credentials
        from google.cloud import storage

        holds("conformance", store_spec.conformance(
            store_gcp, bucket=bucket, client=storage.Client(),
            prefix=f"check/{os.getpid()}/"), f"{len(store_spec.CLAIMS)} claims, on {bucket}")

# ---------------------------------------------------------------------------

print("\nqueue — spec/queue.py")

print("  queue_mem")
cls = offers(queue_mem, queue_spec.QueueM)
if cls is not None and implements(cls, queue_spec.QueueP):
    holds("conformance", queue_spec.conformance(queue_mem),
          f"{len(queue_spec.CLAIMS)} claims")

print("  queue_gcp")
cls = offers(queue_gcp, queue_spec.QueueM)
if cls is not None and implements(cls, queue_spec.QueueP):
    #: `project/location/queue`, the three coordinates a queue has. One
    #: variable rather than three, because two of the three set and the
    #: third missing is a confusing way to be skipped.
    live = os.environ.get("TASKS_QUEUE")
    if live is None:
        say("conformance", SKIP,
            "set TASKS_QUEUE=project/location/queue to run it against a real queue")
    else:  # pragma: no cover - only with credentials
        from google.cloud import tasks_v2

        project, location, name = live.split("/")
        # A paused queue is the safe way to do this: it accepts creation and
        # deletion, which is the whole contract, and dispatches nothing.
        holds("conformance", queue_spec.conformance(
            queue_gcp, project=project, location=location, queue=name,
            base_url="https://example.invalid", client=tasks_v2.CloudTasksClient()),
            f"{len(queue_spec.CLAIMS)} claims, on {name}")

# ---------------------------------------------------------------------------

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("every interface is implemented and every contract holds.")
