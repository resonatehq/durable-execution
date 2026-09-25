"""What a store is: four operations, two errors, and three layers.

    StoreP   a store, once it exists: four operations
    StoreC   how one is made: its configuration in, a store out
    StoreM   a module that offers one, under the name `Store`

`StoreM` is the useful one. A contract cannot be handed a class, because an
implementation may want to choose its class at import time, and it cannot
be handed an instance, because two implementations are configured
differently and only the caller knows how. It is handed the module, and
reaches for `Store`. What each implementation has to do to pass is
`testing/conformance/store.py`.

## The four operations

This is the shape of the thing we actually run on, rather than the shape
our engine happens to want. Google Cloud Storage, S3, R2 and Azure all
offer these four and a version token, and every one of them offers the
conditional writes the whole design rests on. An adapter should be nearly
empty; if it is not, this interface is wrong.

Text in, text out: the engine hands the store the document's JSON, and a
bucket stores bytes, so the store does not interpret the body.

`get` returns the body and its version, or `None` when nothing is there.

`put` writes and returns the new version. The two conditions are exclusive
and passing both is a programming error:

  - `if_match="..."` replaces exactly the version named, and fails otherwise.
  - `if_absent=True` creates, and fails if anything is there.
  - neither is an unconditional overwrite. The engine never uses it; it is
    in the contract because every real store offers it.

`delete` removes, and removing what is not there succeeds. Idempotent,
because a caller that crashed and ran again must not fail.

`list` returns at most `limit` keys under `prefix`, **lexicographically
ascending**, so a zero-padded key sorts in numeric order and a capped
listing is the smallest. Real stores differ here -- S3 and GCS list
lexicographically, while some client libraries promise nothing -- so an
adapter that cannot guarantee it sorts.

## The two errors

They demand opposite responses, which is why they are not one:

  - `Conflict` -- the write did not land and the state has moved.
    The decision was made against something that no longer exists, so it
    must be re-decided. Replaying it would produce answers no sequential
    execution gives.
  - `Unavailable` -- no answer. Nothing is known about whether the write
    landed, so the honest thing is to tell the caller, whose operations are
    all idempotent, and let it retry.

S3 has a third, a 409 for two conditional writes it could not order, which
means "retry this same write". GCS does not, so it is not here; an S3
adapter would add it rather than fold it into either of these.

Neither is in the contract, and that is not an oversight: a store that
fails on demand is a simulator, and a contract every implementation must
pass can only claim what a real one can be asked to do. What an adapter
does with a 429 is checked where an adapter can be made to see one -- in
`test_conformance.py`, against a double.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["StoreP", "StoreC", "StoreM"]


@runtime_checkable
class StoreP(Protocol):
    """A store. The engine is written against this and nothing else."""

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
    """How a store is made -- and the one layer that cannot be pinned down.

    `engine.EngineC` names its arguments, and means it: every engine takes
    the same two ports, because ports are an interface. A store's
    arguments are not an interface, they are a deployment -- the simulated
    one needs nothing, the real one needs a bucket, a client and a prefix,
    and no third implementation will need those either. Forcing a shape on
    them would only mean writing the differences somewhere less honest,
    like a dict.

    So this says the one thing that is true of both: a store is made by
    calling something. Who calls it with what is the caller's business --
    the contract is handed the configuration, and `config.py` reads it
    from the environment.
    """

    def __call__(self, *config: Any, **keywords: Any) -> StoreP: ...


class StoreM(Protocol):
    """A module that offers a store.

    A read-only property rather than a plain attribute: a protocol's
    mutable attribute is invariant, and `Store: StoreC` is a claim no class
    can satisfy. See `engine.py`, and `test_types.py`, which checks it.
    """

    @property
    def Store(self) -> StoreC: ...
