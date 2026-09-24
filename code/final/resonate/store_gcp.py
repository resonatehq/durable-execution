"""A store, for real: Google Cloud Storage.

`store_gcp.Store` is `store.StoreP` over a bucket, and it is almost empty,
which is the test of whether the interface was drawn in the right place.
Everything the design rests on, GCS already offers: a generation per object,
and writes conditioned on it.

    if_generation_match=0      create only if nothing is there
    if_generation_match=<gen>  replace only what was read

A refused precondition comes back as `412`, which is `Conflict`:
the state moved, so the decision must be re-decided and never replayed.
A `429` or a `503` is `Unavailable`: nothing is known about whether the
write landed, which the protocol already tolerates because every operation
is idempotent.

## The one number to design around

Every promise and task of one run lives in one object, so a single object's
write rate is the ceiling on a single run's transitions, not on the system.
A run that needs more than that is a run whose fan-out should be its own
origin. Under many writers the contention surfaces as `Conflict`,
which the caller retries, so the failure mode is latency rather than loss.

Measured against a real bucket on 2026-09-22, rather than assumed:

    30 sequential conditional writes to one object   2.1 writes/sec
      latency p50 432 ms, p95 1253 ms, max 1526 ms
      throttled 0, lost 0

    8 concurrent writers x 5 read-modify-write        2.0 writes/sec
      final counter 40 of 40, LOST UPDATES 0
      412 conflicts 106, i.e. 2.6 retries per success
      throttled 0

Two things in there are worth more than the headline. Eight writers moved
the counter no faster than one did: the object serialises, and the extra
concurrency turned into 106 retries rather than throughput, which is the
argument for making fan-out its own origin, now measured. And nothing was
ever throttled -- 146 write attempts to one object drew no 429 -- so at this
scale the ceiling is round-trip latency, not a quota. Documentation that
quotes "about one write per second per object" describes a limit we did not
reach; we were slower than it for a simpler reason.

## What is verified, and what is not

`store.conformance` passes against a real bucket: all eleven claims, which
is what `spec.check` prints when `GCS_BUCKET` is set, and what
`test_conformance.py::...[gcs]` runs. Generation preconditions behave as
this file reads them -- `if_generation_match=0` creates exactly once, and a
matched generation replaces exactly what was read.

Still resting on documentation: everything about a bucket this code never
asked for. The measurements above are one bucket, one region, one day, from
one machine; the region is not recorded here because the credential used
could not read the bucket's own metadata. Cross-region latency, behaviour
under sustained load far above this, and the queue side (Cloud Tasks) are
unverified -- `spec.check` still prints `skip` for `queue_gcp`, and saying
so is better than implying otherwise.
"""

from __future__ import annotations

from typing import Any

from .errors import Conflict, Unavailable

#: The bytes a document is: one JSON object.
CONTENT_TYPE = "application/json"


class Store:
    """The four operations over a real bucket.

    `client` is injected rather than constructed so a test can hand in a
    double, and so a process that already has one does not make a second.
    """

    def __init__(self, bucket: str, client: Any = None, prefix: str = "") -> None:
        if client is None:  # pragma: no cover - needs credentials
            from google.cloud import storage

            client = storage.Client()
        self.client = client
        self.bucket = client.bucket(bucket)
        self.prefix = prefix

    def _blob(self, key: str):
        return self.bucket.blob(self.prefix + key)

    def get(self, key: str) -> tuple[str, str] | None:
        from google.api_core import exceptions as gcp

        blob = self._blob(key)
        try:
            body = blob.download_as_bytes()
        except gcp.NotFound:
            return None
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
        if blob.generation is None:
            # The library reads the generation off the download's own
            # response header, so this should not happen. If a version ever
            # stops doing that, one more round trip is the right price for
            # not inventing a version number.
            blob.reload()
        return body.decode("utf-8"), str(blob.generation)

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        from google.api_core import exceptions as gcp

        if if_match is not None and if_absent:
            raise ValueError("if_match and if_absent are exclusive")
        blob = self._blob(key)
        precondition: dict = {}
        if if_absent:
            precondition["if_generation_match"] = 0
        elif if_match is not None:
            precondition["if_generation_match"] = int(if_match)
        try:
            blob.upload_from_string(body, content_type=CONTENT_TYPE, **precondition)
        except gcp.PreconditionFailed as e:
            raise Conflict(f"{key}: {e}") from None
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
        return str(blob.generation)

    def delete(self, key: str) -> None:
        from google.api_core import exceptions as gcp

        try:
            self._blob(key).delete()
        except gcp.NotFound:
            pass  # removing what is not there succeeds
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None

    def list(self, prefix: str, limit: int) -> list[str]:
        from google.api_core import exceptions as gcp

        try:
            found = self.client.list_blobs(
                self.bucket, prefix=self.prefix + prefix, max_results=limit)
            names = [b.name[len(self.prefix):] for b in found]
        except (gcp.TooManyRequests, gcp.ServiceUnavailable, gcp.ServerError) as e:
            raise Unavailable(str(e)) from None
        # GCS lists lexicographically, but the contract says so and a client
        # library is free not to, so it is imposed here rather than assumed.
        return sorted(names)[:limit]
