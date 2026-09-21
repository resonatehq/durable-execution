"""A store, for real: Google Cloud Storage.

`store_gcp.Store` is `store.StoreP` over a bucket, and it is almost empty,
which is the test of whether the interface was drawn in the right place.
Everything the design rests on, GCS already offers: a generation per object,
and writes conditioned on it.

    if_generation_match=0      create only if nothing is there
    if_generation_match=<gen>  replace only what was read

A refused precondition comes back as `412`, which is `PreconditionFailed`:
the state moved, so the decision must be re-decided and never replayed.
A `429` or a `503` is `Unavailable`: nothing is known about whether the
write landed, which the protocol already tolerates because every operation
is idempotent.

## The one number to design around

GCS allows roughly one write per second to a single object. Every promise
and task of one run lives in one object, so that is the ceiling on a single
run's transitions, not on the system. A run that needs more than that is a
run whose fan-out should be its own origin. Under many writers it surfaces
as `PreconditionFailed`, which the caller retries, so the failure mode is
latency rather than loss.

## What is not verified

This file has never been run against Google Cloud Storage. It is written
against the library's documented behaviour and against `store.conformance`,
which the simulated store passes and which this will pass or fail the moment
someone sets `GCS_BUCKET` (see `test_conformance.py`). Until then, "it works
on GCS" rests on the documentation, and saying so is better than implying
otherwise.
"""

from __future__ import annotations

from typing import Any

from store import PreconditionFailed, Unavailable

#: The bytes a document is. Not `application/json`: a document is a sequence
#: of JSON values, one per line, which is a different media type and worth
#: being honest about to anything that reads the bucket.
CONTENT_TYPE = "application/x-ndjson"


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
            raise PreconditionFailed(f"{key}: {e}") from None
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
