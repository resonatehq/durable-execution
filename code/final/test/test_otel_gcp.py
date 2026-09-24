"""The edge: our records as OpenTelemetry's objects.

There is one thing in `otel_gcp.py` that can be wrong without failing, and
it is the unit of time. Every clock in this system is milliseconds since
the epoch, because that is what the protocol's `timeoutAt` is;
OpenTelemetry's timestamps are nanoseconds. Get the conversion wrong and
nothing raises -- Cloud Trace simply files every span in 1970 or in the
year 55000, and the first person to notice is looking for something else.

The rest of the file is a check that the ids survive the trip. They are
bytes here and 128- and 64-bit integers there, and an endianness that
disagreed with itself would break the parent links while leaving every
single-span trace looking perfect.
"""

from __future__ import annotations

import datetime

import pytest

pytest.importorskip("opentelemetry.sdk.trace",
                    reason="pip install opentelemetry-sdk opentelemetry-exporter-gcp-trace")

from resonate import otel  # noqa: E402
from resonate import otel_gcp  # noqa: E402

#: A real instant with a fractional second, so a truncation shows up.
START = 1_758_700_000_123
END = START + 364


def resource():
    from opentelemetry.sdk.resources import Resource
    return Resource.create({"service.name": "test"})


def convert(span: otel.Span):
    return otel_gcp._readable(span, resource())


def a_span(**kw) -> otel.Span:
    base = dict(name="research", trace=otel.trace_id("research.1"),
                span=otel.span_id("research.1"), parent=None,
                start_ms=START, end_ms=END, status=otel.OK,
                attributes={"de.span": "logical", "de.promise": "research.1"})
    return otel.Span(**{**base, **kw})


def test_milliseconds_become_nanoseconds():
    r = convert(a_span())
    when = datetime.datetime.fromtimestamp(r.start_time / 1e9, datetime.timezone.utc)
    assert 2020 < when.year < 2100, f"{when}: the unit is wrong by a factor of a million"
    assert r.end_time - r.start_time == 364 * 1_000_000
    assert r.start_time % 1_000_000_000 == 123_000_000, "the fractional second was lost"


def test_the_ids_survive_the_change_of_type():
    """Bytes here, integers there. An endianness that disagreed with itself
    would still link a span to its own parent and break every other link."""
    parent = otel.span_id("research.1")
    r = convert(a_span(span=otel.span_id("research.1:2"), parent=parent))
    assert r.context.trace_id.to_bytes(16, "big") == otel.trace_id("research.1")
    assert r.context.span_id.to_bytes(8, "big") == otel.span_id("research.1:2")
    assert r.parent.span_id.to_bytes(8, "big") == parent
    assert r.parent.trace_id == r.context.trace_id, "the parent is in another trace"


def test_a_root_span_has_no_parent():
    assert convert(a_span(parent=None)).parent is None


def test_every_span_is_sampled():
    """Sampling here is the sink being on, not a coin flip per span. A trace
    missing the attempt that failed is the one thing it cannot be missing."""
    assert convert(a_span()).context.trace_flags.sampled


@pytest.mark.parametrize("ours,theirs", [
    (otel.OK, "OK"), (otel.ERROR, "ERROR"), (otel.UNSET, "UNSET"),
])
def test_the_three_outcomes_map(ours, theirs):
    assert convert(a_span(status=ours)).status.status_code.name == theirs


def test_a_flush_that_fails_does_not_fail_the_caller():
    """The whole reason this is not a third port. A span is what you look at
    when something else went wrong; if it could raise, the observability
    could take the system down."""
    class Broken:
        def export(self, spans):
            raise ConnectionError("no route to Cloud Trace")

    exporter = otel_gcp.Exporter.__new__(otel_gcp.Exporter)
    exporter.__dict__.update(batch=2, lock=__import__("threading").Lock(),
                             pending=[], dropped=0, sent=0,
                             resource=resource(), gcp=Broken())
    exporter(a_span())
    exporter(a_span())  # reaches the batch size, so it tries to send
    assert exporter.dropped == 2 and exporter.sent == 0


def test_a_batch_is_held_until_it_is_full():
    sent = []

    class Counting:
        def export(self, spans):
            sent.append(len(spans))

    exporter = otel_gcp.Exporter.__new__(otel_gcp.Exporter)
    exporter.__dict__.update(batch=3, lock=__import__("threading").Lock(),
                             pending=[], dropped=0, sent=0,
                             resource=resource(), gcp=Counting())
    for _ in range(4):
        exporter(a_span())
    assert sent == [3], "a request per span would cost more than the run"
    exporter.flush()
    assert sent == [3, 1] and exporter.sent == 4


# --- the whole way out -----------------------------------------------------


class FakeTrace:
    """Cloud Trace's client, as far as the exporter can tell. The library
    builds protobufs and calls one method; this keeps what it was given."""

    def __init__(self) -> None:
        self.batches: list = []

    def batch_write_spans(self, request=None, **kw):
        # The library passes a `BatchWriteSpansRequest` protobuf, not a dict.
        got = request if request is not None else kw
        self.batches.append(list(getattr(got, "spans", None) or got["spans"]))


def a_whole_run():
    """The README's agent, run to completion, with its spans collected."""
    from exampleapp import load

    demo = load("research-agent")
    from resonate.engine import Engine
    from resonate.kernel import KernelCfg
    from resonate.queue_mem import Queue
    from resonate.runtime import Clock, Runtime, Worker
    from resonate.store_mem import Store

    class Ticking(Clock):
        """Moves a little on every read, so the spans have width."""
        def __call__(self):
            self.now += 7
            return self.now

    store, queue, clock = Store(), Queue(), Ticking()
    engine = Engine(store, queue, KernelCfg(retry_timeout=30_000))
    rt = Runtime(engine, queue, clock)
    rt.serve("worker://w", Worker(engine, clock, "w-1"),
             demo.research, demo.search, demo.agent)
    with otel.collecting() as spans:
        rt.start("research.1", demo.research, "What is durable execution?")
        rt.drain()
    return spans


def test_the_library_accepts_a_whole_run():
    """Everything short of the network, on a real run rather than a fixture.

    The library does its own translation into Cloud Trace's protobufs, and
    that is where an attribute of a type it will not carry, an id of the
    wrong width, or an end before its start stops being our problem and
    starts being a rejected batch in production. `test_otel.py` cannot see
    any of it: it asserts our records, and this asserts what becomes of
    them.
    """
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.resources import Resource

    spans = a_whole_run()
    assert len(spans) == 13, len(spans)

    client = FakeTrace()
    exporter = CloudTraceSpanExporter(project_id="p", client=client)
    result = exporter.export([otel_gcp._readable(s, Resource.create({})) for s in spans])
    assert result.name == "SUCCESS", result

    sent = client.batches[0]
    assert len(sent) == len(spans), "the library dropped some"

    # One trace, and every parent link still resolves after the round trip
    # through hex strings -- which is the form Cloud Trace actually stores.
    traces = {s.name.split("/traces/")[1].split("/")[0] for s in sent}
    assert traces == {otel.trace_id("research.1").hex()}
    ids = {s.span_id for s in sent}
    for s in sent:
        assert not s.parent_span_id or s.parent_span_id in ids, s.name

    assert {s.display_name.value for s in sent} == {"research", "agent", "search"}


def test_the_run_reads_as_two_layers_at_the_far_end():
    """The pair, after everything: one promise, more than one attempt."""
    spans = a_whole_run()
    by = {}
    for s in spans:
        by.setdefault(s.attributes["de.promise"], []).append(s.attributes["de.span"])
    assert by["research.1"].count("logical") == 1
    assert by["research.1"].count("physical") == 2, by["research.1"]

    root = next(s for s in spans if s.attributes["de.promise"] == "research.1"
                and s.attributes["de.span"] == "logical")
    worked = sum(s.duration_ms for s in spans
                 if s.attributes["de.promise"] == "research.1"
                 and s.attributes["de.span"] == "physical")
    assert root.duration_ms > worked, (
        "the run was never idle, so the two layers said the same thing")
