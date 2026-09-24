"""The other end of `otel.py`: spans, in Cloud Trace.

`store_gcp.py` and `queue_gcp.py` are the two ports against Google. This is
not a third -- nothing in the engine depends on it, and a deployment with
it switched off behaves identically. It is an adapter at the edge, and it
is the only file in the project that imports OpenTelemetry.

That separation is the point. `otel.py` derives the ids, which is the part
worth testing, and it does so as plain data: `test_otel.py` asserts every
claim about the trace without a tracer provider, an exporter, a batch
processor or a shutdown hook. This file turns those records into the
library's objects and hands them over, and there is nothing in it to get
subtly wrong except the unit of time, which has a test of its own.

## Why the spans are built rather than started

The usual way to produce a span is `tracer.start_as_current_span(...)`,
which mints an id and takes its parent from whatever is current. Both of
those are exactly what this system must not do: the ids are derived from
durable ids so that a container which joins a run three days late lands in
the same trace, and there is no ambient parent to inherit because the
parent ran in another process. So the spans arrive already built, with
their ids and their ends already decided, and this constructs the
`ReadableSpan` an exporter consumes directly.

## The exporter is deprecated, and is used anyway

`CloudTraceSpanExporter` carries a deprecation warning as of
`opentelemetry-exporter-gcp-trace` 1.15.0, which is its latest release.
Google's migration guide points at OTLP against `telemetry.googleapis.com`
instead.

It is used regardless, for one reason: this path has been driven
end-to-end -- a whole run of the research agent, through the library's own
translation into Cloud Trace's protobufs, thirteen spans accepted with
every parent link intact -- and the OTLP path has not. Swapping a verified
adapter for an unverified one to avoid a warning is a trade in the wrong
direction, and the swap is about twenty lines when somebody can test it.
`test_otel_gcp.py` is what would grade the replacement, and `otel.py` does
not change either way.

## Sending

`CloudTraceSpanExporter` batches per call, so spans are buffered and
flushed rather than sent one at a time -- one run of the research agent is
thirteen spans and thirteen round trips would cost more than the run. A
flush that fails is logged and dropped: a trace is worth having and never
worth failing a request for, which is the difference between this and the
two real ports.

    PROJECT=... TRACE=1 python -m your.entrypoint

`app.py` reads `TRACE` and calls `install()`. Nothing else refers to this.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any

import otel

log = logging.getLogger(__name__)

#: How many spans to hold before sending. A run is a dozen or so, and a
#: container serving many at once should not make a request per span.
BATCH = 64


def _readable(span: otel.Span, resource: Any) -> Any:
    """One of ours as one of theirs.

    Milliseconds in, nanoseconds out: OpenTelemetry timestamps are
    nanoseconds since the epoch and this system's clocks are milliseconds
    since the epoch, everywhere, because that is what the protocol's
    `timeoutAt` is. Getting this wrong does not fail -- it puts every span
    in 1970, or in the year 55000 -- so `test_otel_gcp.py` asserts it.
    """
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
    from opentelemetry.trace.status import Status, StatusCode

    def context(trace: bytes, span_: bytes) -> SpanContext:
        return SpanContext(
            trace_id=int.from_bytes(trace, "big"),
            span_id=int.from_bytes(span_, "big"),
            is_remote=False,
            # Sampled, because this system decides what to record by turning
            # the sink on, not by rolling dice per span. A trace missing the
            # attempt that failed is the one thing it cannot be missing.
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

    status = {otel.OK: StatusCode.OK, otel.ERROR: StatusCode.ERROR}.get(
        span.status, StatusCode.UNSET)
    return ReadableSpan(
        name=span.name,
        context=context(span.trace, span.span),
        parent=context(span.trace, span.parent) if span.parent else None,
        resource=resource,
        attributes={k: v for k, v in span.attributes.items() if v is not None},
        kind=SpanKind.INTERNAL,
        status=Status(status),
        start_time=span.start_ms * 1_000_000,
        end_time=span.end_ms * 1_000_000,
        instrumentation_scope=InstrumentationScope("durable-execution", "1"),
    )


class Exporter:
    """Buffer spans, send them in batches, never fail a request.

    A lock rather than a queue and a thread: a container serving eighty
    requests at once appends from eighty threads, and the flush is a
    blocking HTTP call that the thread which filled the buffer pays for.
    That is the cheap version, and it is honest about being the cheap
    version -- the expensive one is `BatchSpanProcessor`, which is worth
    having when the flush latency shows up in a request and not before.
    """

    def __init__(self, project: str | None = None, batch: int = BATCH) -> None:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource

        self.batch = batch
        self.lock = threading.Lock()
        self.pending: list[otel.Span] = []
        self.dropped = 0
        self.sent = 0
        self.resource = Resource.create({
            "service.name": os.environ.get("K_SERVICE", "durable-execution"),
            "service.instance.id": os.environ.get("K_REVISION", "local"),
        })
        self.gcp = CloudTraceSpanExporter(project_id=project)

    def __call__(self, span: otel.Span) -> None:
        with self.lock:
            self.pending.append(span)
            if len(self.pending) < self.batch:
                return
            batch, self.pending = self.pending, []
        self.send(batch)

    def flush(self) -> None:
        with self.lock:
            batch, self.pending = self.pending, []
        if batch:
            self.send(batch)

    def send(self, batch: list[otel.Span]) -> None:
        try:
            self.gcp.export([_readable(s, self.resource) for s in batch])
            self.sent += len(batch)
        except Exception as e:
            # Deliberately swallowed. A span is a thing you look at when
            # something else went wrong; making it able to fail the request
            # would mean the observability could take the system down.
            self.dropped += len(batch)
            log.warning("dropped %d spans: %s", len(batch), e)


def install(project: str | None = None) -> Exporter:
    """Send this process's spans to Cloud Trace, until it exits."""
    exporter = Exporter(project or os.environ.get("PROJECT"))
    otel.to(exporter)
    atexit.register(exporter.flush)
    return exporter
