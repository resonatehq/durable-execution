"""What a store is, and what it has to do to be one.

Three layers, the same three `spec.py` draws around an engine, because
there are three things to name and they are not the same thing:

    StoreP   a store, once it exists: four operations
    StoreC   how one is made: its configuration in, a store out
    StoreM   a module that offers one, under the name `Store`

`StoreM` is the useful one. A contract cannot be handed a class, because an
implementation may want to choose its class at import time, and it cannot
be handed an instance, because two implementations are configured
differently and only the caller knows how. It is handed the module, and
reaches for `Store`.

    from store import conformance
    import store_mem, store_gcp

    assert conformance(store_mem) == []
    assert conformance(store_gcp, bucket="runs", prefix="t/") == []

Which is why the contract lives here rather than inside either
implementation: a suite that shipped with one of them would be grading the
other against a rival instead of against a contract.

## The four operations

This is the shape of the thing we actually run on, rather than the shape
our engine happens to want. Google Cloud Storage, S3, R2 and Azure all
offer these four and a version token, and every one of them offers the
conditional writes the whole design rests on. An adapter should be nearly
empty; if it is not, this interface is wrong.

JSON in, JSON out. Not because a bucket cares — it stores bytes — but
because a seam where the value is a string of JSON is a seam something else
can stand at and read. A store that can decode what passes through it can
check it, and the checking gets the one thing a test cannot buy: it happens
on every write in every run, including the ones nobody wrote with checking
in mind.

`get` returns the body and its version, or `None` when nothing is there.

`put` writes and returns the new version. The two conditions are exclusive
and passing both is a programming error:

  - `if_match="..."` replaces exactly the version named, and fails otherwise.
  - `if_absent=True` creates, and fails if anything is there.
  - neither is an unconditional overwrite, which is only ever correct when
    the *key* carries the whole value — a deadline named by its instant,
    say, where writing the same key twice is writing the same fact twice.

`delete` removes, and removing what is not there succeeds. Idempotent,
because a collector that crashed and ran again must not fail.

`list` returns at most `limit` keys under `prefix`, **lexicographically
ascending**. The order is load-bearing rather than incidental: a deadline
zero-padded into its key sorts into time order, so the nearest deadlines
are a capped listing and finding what is due costs no index. Real stores
differ here — S3 and GCS list lexicographically, while some client
libraries promise nothing — so an adapter that cannot guarantee it sorts.

## The two errors

They demand opposite responses, which is why they are not one:

  - `PreconditionFailed` — the write did not land and the state has moved.
    The decision was made against something that no longer exists, so it
    must be re-decided. Replaying it would produce answers no sequential
    execution gives.
  - `Unavailable` — no answer. Nothing is known about whether the write
    landed, so the honest thing is to tell the caller, whose operations are
    all idempotent, and let it retry.

S3 has a third, a 409 for two conditional writes it could not order, which
means "retry this same write". GCS does not, so it is not here; an S3
adapter would add it rather than fold it into either of these.

Neither is in the contract below, and that is not an oversight: a store
that fails on demand is a simulator, and a contract every implementation
must pass can only claim what a real one can be asked to do. What an
adapter does with a 429 is checked where an adapter can be made to see
one — in `test_conformance.py`, against a double.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Protocol, runtime_checkable

from ports import Conflict, Unavailable, Violation

#: A refused precondition. The state moved: re-decide, never replay. Spelled
#: as the engine's `Conflict` so there is one name for it in the codebase.
PreconditionFailed = Conflict

__all__ = ["StoreP", "StoreC", "StoreM", "PreconditionFailed", "Unavailable",
           "conformance", "CLAIMS"]


# ---------------------------------------------------------------------------
# The three layers
# ---------------------------------------------------------------------------


@runtime_checkable
class StoreP(Protocol):
    def get(self, key: str) -> tuple[str, str] | None:
        """The body and its version, or `None`."""

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        """Write, conditionally, and return the new version."""

    def delete(self, key: str) -> None:
        """Remove. Removing what is not there succeeds."""

    def list(self, prefix: str, limit: int) -> list[str]:
        """At most `limit` keys under `prefix`, lexicographically ascending."""


class StoreC(Protocol):
    """How a store is made.

    Configuration is keywords rather than a fixed signature because there
    is nothing in common to fix: the simulated store needs nothing, and the
    real one needs a bucket, a client and a prefix. What *is* fixed is that
    whoever runs the contract supplies it, which is the same seam that lets
    one engine run over a bucket in production and a dict in a simulation.
    """

    def __call__(self, **config: Any) -> StoreP: ...


class StoreM(Protocol):
    """A module that offers a store."""

    Store: StoreC


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: Every claim, in order. A claim is handed a store and a prefix of its own,
#: so the claims neither collide nor depend on each other, and so tidying up
#: after one is a listing rather than bookkeeping.
CLAIMS: list[tuple[str, Callable[[StoreP, str], None]]] = []


def claim(what: str):
    def keep(fn: Callable[[StoreP, str], None]):
        CLAIMS.append((what, fn))
        return fn
    return keep


@contextmanager
def refused(error: type[BaseException], why: str):
    """The claim is that this is refused, and refused *that* way. Another
    error is a different failure and propagates, because a store that raises
    the wrong thing is not a store that passed."""
    try:
        yield
    except error:
        return
    raise AssertionError(why)


@claim("nothing is there until something is")
def _absent(store: StoreP, p: str) -> None:
    assert store.get(p + "o") is None


@claim("a create wins once")
def _create(store: StoreP, p: str) -> None:
    version = store.put(p + "o", "one", if_absent=True)
    assert store.get(p + "o") == ("one", version)
    with refused(PreconditionFailed, "a second create was allowed"):
        store.put(p + "o", "two", if_absent=True)
    assert store.get(p + "o")[0] == "one", "the loser's bytes landed anyway"


@claim("a replacement needs the version it read")
def _replace(store: StoreP, p: str) -> None:
    first = store.put(p + "o", "one", if_absent=True)
    second = store.put(p + "o", "two", if_match=first)
    assert second != first, "the version did not change with the object"
    with refused(PreconditionFailed, "a stale version was accepted"):
        store.put(p + "o", "three", if_match=first)
    assert store.get(p + "o") == ("two", second)


@claim("a replacement of nothing is refused")
def _replace_nothing(store: StoreP, p: str) -> None:
    with refused(PreconditionFailed, "replacing what is not there was allowed"):
        store.put(p + "o", "one", if_match="1")


@claim("the two conditions are exclusive")
def _exclusive(store: StoreP, p: str) -> None:
    with refused(ValueError, "if_match and if_absent were accepted together"):
        store.put(p + "o", "one", if_match="1", if_absent=True)


@claim("an unconditional write needs no version")
def _unconditional(store: StoreP, p: str) -> None:
    store.put(p + "o", "one")
    store.put(p + "o", "two")
    assert store.get(p + "o")[0] == "two"


@claim("removing is idempotent")
def _delete(store: StoreP, p: str) -> None:
    store.delete(p + "gone")  # a collector that crashed and ran again
    store.put(p + "o", "one", if_absent=True)
    store.delete(p + "o")
    store.delete(p + "o")
    assert store.get(p + "o") is None


@claim("a key is free again once it is removed")
def _recreate(store: StoreP, p: str) -> None:
    store.put(p + "o", "one", if_absent=True)
    store.delete(p + "o")
    store.put(p + "o", "two", if_absent=True)
    assert store.get(p + "o")[0] == "two"


@claim("listing is lexicographic ascending and capped")
def _list(store: StoreP, p: str) -> None:
    for at in (300, 10, 2_000, 45):
        store.put(f"{p}t/{at:020d}_o", "")
    store.put(p + "wf/o", "{}")
    want = [f"{p}t/{at:020d}_o" for at in (10, 45, 300, 2_000)]
    assert store.list(p + "t/", 10) == want
    assert store.list(p + "t/", 2) == want[:2], "the limit was not a limit"
    assert store.list(p + "wf/", 10) == [p + "wf/o"], "the prefix was not a filter"


@claim("a document survives the round trip unchanged")
def _round_trip(store: StoreP, p: str) -> None:
    body = '{"t":"h","v":1}\n{"t":"o","id":"o:\\u00e9"}'
    store.put(p + "o", body, if_absent=True)
    assert store.get(p + "o")[0] == body


@claim("a version is opaque and belongs to one key")
def _versions(store: StoreP, p: str) -> None:
    a = store.put(p + "a", "one", if_absent=True)
    b = store.put(p + "b", "one", if_absent=True)
    assert isinstance(a, str) and a, "a version is a non-empty string"
    with refused(PreconditionFailed, "another key's version was accepted"):
        store.put(p + "a", "two", if_match=b)


def conformance(module: StoreM, **config: Any) -> list[Violation]:
    """Drive `module.Store` through every claim and return what it broke.

    Every claim gets its own prefix and is cleaned up after, so a store that
    fails one is still graded on the rest, and a run against a real bucket
    leaves nothing behind.
    """
    store = module.Store(**config)
    out: list[Violation] = []
    for i, (what, check) in enumerate(CLAIMS):
        prefix = f"conformance/{i}/"
        try:
            check(store, prefix)
        except AssertionError as e:
            out.append(Violation(i, what, str(e) or "the claim did not hold"))
        except Exception as e:
            out.append(Violation(i, what, f"{type(e).__name__}: {e}"))
        finally:
            try:
                for key in store.list(prefix, 1_000):
                    store.delete(key)
            except Exception:
                pass  # a store too broken to tidy after is already failing
    return out
