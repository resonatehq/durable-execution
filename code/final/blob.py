"""The bucket, as four methods.

This is the shape of the thing we are actually going to run on, rather than
the shape our engine happens to want. Google Cloud Storage, S3, R2 and Azure
all offer these four operations and a version token, and every one of them
offers the conditional writes the whole design rests on. An adapter to a real
bucket should be nearly empty; if it is not, this interface is wrong.

JSON in, JSON out. Not because a bucket cares — it stores bytes — but because
a seam where the value is a string of JSON is a seam something else can stand
at and read. A store that can decode what passes through it can check it, and
the checking gets the one thing a test cannot buy: it happens on every write
in every run, including the ones nobody wrote with checking in mind.

## The contract

`get` returns the body and its version, or `None` when nothing is there.

`put` writes and returns the new version. The two conditions are exclusive
and passing both is a programming error:

  - `if_match="..."` replaces exactly the version named, and fails otherwise.
  - `if_absent=True` creates, and fails if anything is there.
  - neither is an unconditional overwrite, which is only ever correct when
    the *key* carries the whole value — a deadline named by its instant, say,
    where writing the same key twice is writing the same fact twice.

`delete` removes, and removing what is not there succeeds. Idempotent,
because a collector that crashed and ran again must not fail.

`list` returns at most `limit` keys under `prefix`, **lexicographically
ascending**. The order is load-bearing rather than incidental: a deadline
zero-padded into its key sorts into time order, so the nearest deadlines are
a capped listing and finding what is due costs no index. Real stores differ
here — S3 and GCS list in lexicographic order, while some client libraries
promise nothing — so an adapter that cannot guarantee it must sort.

## The errors

Two, and they demand opposite responses, which is why they are not one:

  - `PreconditionFailed` — the write did not land and the state has moved.
    The decision was made against something that no longer exists, so it must
    be re-decided. Replaying it would produce answers no sequential execution
    gives.
  - `Unavailable` — no answer. Nothing is known about whether the write
    landed, so the honest thing is to tell the caller, whose operations are
    all idempotent, and let it retry.

S3 has a third, a 409 for two conditional writes it could not order, which
means "retry this same write". GCS does not, so it is not here; an S3
adapter would add it rather than fold it into either of these.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ports import Conflict, Fault, Unavailable

#: A refused precondition. The state moved: re-decide, never replay. Spelled
#: as the store's `Conflict` so there is one name for it in the codebase.
PreconditionFailed = Conflict

__all__ = ["Blob", "MemoryBlob", "BlobStore", "PreconditionFailed", "Unavailable"]


@runtime_checkable
class Blob(Protocol):
    def get(self, key: str) -> tuple[str, str] | None:
        """The body and its version, or `None`."""

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        """Write, conditionally, and return the new version."""

    def delete(self, key: str) -> None:
        """Remove. Removing what is not there succeeds."""

    def list(self, prefix: str, limit: int) -> list[str]:
        """At most `limit` keys under `prefix`, lexicographically ascending."""


class MemoryBlob:
    """A bucket in a dict, with the semantics that matter kept honest.

    Versions are opaque strings and deliberately not numbers a caller could
    do arithmetic on: an adapter over an ETag has nothing to count with, so
    neither does this.
    """

    def __init__(self, fault: Fault | None = None) -> None:
        self._objects: dict[str, tuple[str, str]] = {}
        self._n = 0
        #: Where the power goes out. A bucket is the thing most likely to
        #: stop answering mid-write, so the simulated one can.
        self.fault = fault

    def get(self, key: str) -> tuple[str, str] | None:
        return self._objects.get(key)

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        if if_match is not None and if_absent:
            raise ValueError("if_match and if_absent are exclusive")
        current = self._objects.get(key)
        if if_absent and current is not None:
            raise PreconditionFailed(f"{key} already exists")
        if if_match is not None:
            if current is None:
                raise PreconditionFailed(f"{key} does not exist")
            if current[1] != if_match:
                raise PreconditionFailed(f"{key} is at {current[1]}, not {if_match}")
        if self.fault is not None:
            self.fault.tick(f"put {key}")
        self._n += 1
        version = f"v{self._n}"
        self._objects[key] = (body, version)
        return version

    def delete(self, key: str) -> None:
        if self.fault is not None:
            self.fault.tick(f"delete {key}")
        self._objects.pop(key, None)

    def list(self, prefix: str, limit: int) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))[:limit]


class BlobStore:
    """The engine's `Store` over a `Blob`.

    The engine wants one thing — read a document, replace exactly what it
    read — and the bucket offers four. This is the whole of the difference,
    and it is where "no version yet" becomes the empty string, so the engine
    can talk about a document that does not exist yet without a special case.
    """

    #: What `load` reports for a key that holds nothing, and what `commit`
    #: reads as "create this".
    ABSENT = ""

    def __init__(self, blob: Blob) -> None:
        self.blob = blob

    def load(self, key: str) -> tuple[bytes | None, str]:
        found = self.blob.get(key)
        if found is None:
            return None, self.ABSENT
        body, version = found
        return body.encode("utf-8"), version

    def commit(self, key: str, body: bytes, if_generation: str) -> str:
        text = body.decode("utf-8")
        if if_generation == self.ABSENT:
            return self.blob.put(key, text, if_absent=True)
        return self.blob.put(key, text, if_match=if_generation)
