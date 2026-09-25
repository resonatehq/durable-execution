"""What a store has to do to be one: eleven claims, in order.

`spec/store.py` says what a store *is* -- the four operations, the two
errors, and what each one means. This is the part a bucket can fail.

    from resonate.testing.conformance.store import conformance
    from resonate import store_gcp
    from resonate.testing import store_mem

    assert conformance(store_mem) == []
    assert conformance(store_gcp, bucket="runs", prefix="t/") == []

The suite is handed a module rather than a class or an instance, for the
reason `StoreM` gives: only the caller knows how a particular store is
configured. It lives here rather than beside either implementation because
a suite that shipped with one of them would be grading the other against a
rival instead of against a contract.

What it can claim is only what a real store can be asked to do without
being watched. Refusals it cannot provoke -- a 429, a 503 -- are checked
against a double in `test_conformance.py` instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable

from ...errors import Conflict
from ...spec.store import StoreM, StoreP
from .violation import Violation

__all__ = ["conformance", "CLAIMS"]


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
    with refused(Conflict, "a second create was allowed"):
        store.put(p + "o", "two", if_absent=True)
    assert store.get(p + "o")[0] == "one", "the loser's bytes landed anyway"


@claim("a replacement needs the version it read")
def _replace(store: StoreP, p: str) -> None:
    first = store.put(p + "o", "one", if_absent=True)
    second = store.put(p + "o", "two", if_match=first)
    assert second != first, "the version did not change with the object"
    with refused(Conflict, "a stale version was accepted"):
        store.put(p + "o", "three", if_match=first)
    assert store.get(p + "o") == ("two", second)


@claim("a replacement of nothing is refused")
def _replace_nothing(store: StoreP, p: str) -> None:
    with refused(Conflict, "replacing what is not there was allowed"):
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
    store.delete(p + "gone")  # a caller that crashed and ran again
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
    with refused(Conflict, "another key's version was accepted"):
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
