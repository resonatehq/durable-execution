"""What a read and a write actually cost, against a real bucket.

`store_gcp.py` records a write rate -- about two per second to one object --
and nothing about reads, which is half the question. A transition is one
`get` and one `put`: the engine reads the document, decides, and writes once
under a precondition. So the floor on how fast a run can proceed is the sum
of the two, and only one of them has ever been measured.

This measures the operations the engine actually calls, through
`store_gcp.Store` rather than the library underneath it, so what comes out
is what the engine experiences including our own decoding.

    GOOGLE_APPLICATION_CREDENTIALS=key.json python live/latency.py

It writes under `latency/<pid>/` and deletes what it made.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from resonate import store_gcp  # noqa: E402
from resonate.spec.store import PreconditionFailed  # noqa: E402

BUCKET = os.environ.get("GCS_BUCKET", "de-contract-28425")
N = int(os.environ.get("N", 30))

#: About the size of a finished research document. Latency is not flat in
#: object size, and measuring an empty one would flatter the result.
BODY = "x" * 2000


def timed(fn, n: int) -> list[float]:
    """Milliseconds per call, first call separate.

    The first call pays for a TLS handshake and a token mint, which a
    long-lived container pays once and a cold start pays every time. Mixing
    it into the percentiles hides both.
    """
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


def report(name: str, ms: list[float]) -> None:
    rest = sorted(ms[1:]) or sorted(ms)
    print(f"{name:22} first {ms[0]:7.0f} ms   "
          f"p50 {statistics.median(rest):6.0f}   "
          f"p95 {rest[int(len(rest) * 0.95) - 1]:6.0f}   "
          f"max {rest[-1]:6.0f}   n={len(rest)}")


def main() -> int:
    from google.cloud import storage

    store = store_gcp.Store(BUCKET, client=storage.Client(),
                            prefix=f"latency/{os.getpid()}/")
    print(f"bucket {BUCKET}, {N} samples, {len(BODY)} byte documents\n")

    # A miss: what the engine sees on the first transition of every run.
    miss = timed(lambda: store.get("absent"), N)

    # A create, then repeated conditional replaces -- the shape of a run.
    version = store.put("doc", BODY, if_absent=True)
    hit = timed(lambda: store.get("doc"), N)

    puts = []
    for _ in range(N):
        t = time.perf_counter()
        version = store.put("doc", BODY, if_match=version)
        puts.append((time.perf_counter() - t) * 1000)

    # A refused precondition: what contention costs, which is not the same
    # as a successful write and is the path a busy origin takes.
    refused = []
    for _ in range(min(N, 10)):
        t = time.perf_counter()
        try:
            store.put("doc", BODY, if_match="1")
        except PreconditionFailed:
            pass
        refused.append((time.perf_counter() - t) * 1000)

    report("get (miss)", miss)
    report("get (hit)", hit)
    report("put (if_match)", puts)
    report("put (refused 412)", refused)

    g = statistics.median(sorted(hit[1:]))
    p = statistics.median(sorted(puts[1:]))
    rate = f"{1000 / (g + p):.1f}" if g + p else "-- (too fast to divide; a double?)"
    print(f"\na transition is one get and one put: {g:.0f} + {p:.0f} = "
          f"{g + p:.0f} ms, so about {rate} transitions/sec")
    print("That is the floor for one origin. It is a latency bound, not a")
    print("quota: nothing here is throttled, the writes are simply serial.")

    store.delete("doc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
