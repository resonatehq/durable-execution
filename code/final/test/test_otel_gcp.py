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

import otel  # noqa: E402
import otel_gcp  # noqa: E402

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
