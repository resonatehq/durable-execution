"""A store in a dict, with the semantics that matter kept honest.

This is what every test in the project runs on, so it is not a toy: the
conditional writes are real, the versions are opaque, and the power can go
out in the middle. `store.conformance(store_mem)` holds it to the same
contract as the bucket.

Versions are strings and deliberately not numbers a caller could do
arithmetic on: an adapter over an ETag has nothing to count with, so
neither does this.
"""

from __future__ import annotations

from ..errors import Conflict
from .faults import Fault


class Store:
    def __init__(self, fault: Fault | None = None) -> None:
        self.objects: dict[str, tuple[str, str]] = {}
        self._n = 0
        #: Where the power goes out. A store is the thing most likely to
        #: stop answering mid-write, so the simulated one can.
        self.fault = fault
        #: When set, a faulted write lands and *then* raises, which is the
        #: window where nothing is known about whether the write landed.
        #: The other order — raise before writing — is the ordinary one and
        #: is what `fault` alone does.
        self.land_then_fail = False

    def get(self, key: str) -> tuple[str, str] | None:
        """Reads never fault, so a test can see exactly what landed after
        the power went out."""
        return self.objects.get(key)

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        if if_match is not None and if_absent:
            raise ValueError("if_match and if_absent are exclusive")
        current = self.objects.get(key)
        if if_absent and current is not None:
            raise Conflict(f"{key} already exists")
        if if_match is not None:
            if current is None:
                raise Conflict(f"{key} does not exist")
            if current[1] != if_match:
                raise Conflict(f"{key} is at {current[1]}, not {if_match}")
        self._n += 1
        version = f"v{self._n}"
        if self.land_then_fail and self.fault is not None and self.fault.budget == 0:
            self.objects[key] = (body, version)
        if self.fault is not None:
            self.fault.tick(f"commit {key}")
        self.objects[key] = (body, version)
        return version

    def delete(self, key: str) -> None:
        if self.fault is not None:
            # "remove", not "delete": the write log is read by
            # `spec._effect_order`, where a "delete" is a deadline being
            # disarmed. The engine never removes a document, but a log
            # nobody can misread is worth one word.
            self.fault.tick(f"remove {key}")
        self.objects.pop(key, None)

    def list(self, prefix: str, limit: int) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))[:limit]
