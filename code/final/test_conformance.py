"""Every implementation, against the contract its interface publishes.

`store.conformance` and `timer.conformance` are the claims; this file is
the list of things to run them against. The simulated ones run always. The
real store runs when `GCS_BUCKET` names a bucket this machine can reach,
and is skipped otherwise, so a green suite never implies a live one.

Between those two there is a third: the real adapter over a double that
raises the library's own exceptions. It cannot say whether Google Cloud
Storage behaves as documented, but it does say whether our adapter calls
what it meant to, spells the preconditions the library's way, and turns
each failure into the right one of ours. Those are the mistakes an adapter
actually makes.

Below the contracts are the claims a contract cannot make: what a 429
becomes, what a task's request looks like on the wire, what the horizon
does. A contract may only claim what every implementation can be asked to
do without being watched; these need a double, so they live here.
"""

from __future__ import annotations

import json
import os

import pytest

#: The adapters' own tests, and only those, need Google's libraries.
#: Skipping rather than failing keeps `requirements-dev.txt` honest about
#: what the kernel and the simulators depend on, which is nothing.
gcp = pytest.importorskip("google.api_core.exceptions")

import store as store_spec
import store_gcp
import store_mem
import timer as timer_spec
import timer_gcp
import timer_mem
from ports import Unavailable
from store_gcp import CONTENT_TYPE
from timer_gcp import HORIZON_MS


# ---------------------------------------------------------------------------
# A double for the storage library
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name, self._properties = bucket, name, {}

    @property
    def generation(self):
        g = self._properties.get("generation")
        return int(g) if g is not None else None

    def download_as_bytes(self):
        self.bucket.fail_if_asked()
        found = self.bucket.objects.get(self.name)
        if found is None:
            raise gcp.NotFound(self.name)
        body, gen = found
        self._properties["generation"] = gen  # as the real one does, from a header
        return body.encode()

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self.bucket.fail_if_asked()
        assert content_type == CONTENT_TYPE, "a document is not application/json"
        current = self.bucket.objects.get(self.name)
        have = current[1] if current else 0
        if if_generation_match is not None and int(if_generation_match) != have:
            raise gcp.PreconditionFailed(f"generation is {have}")
        self.bucket.seq += 1
        self.bucket.objects[self.name] = (data, self.bucket.seq)
        self._properties["generation"] = self.bucket.seq

    def delete(self):
        self.bucket.fail_if_asked()
        if self.name not in self.bucket.objects:
            raise gcp.NotFound(self.name)
        del self.bucket.objects[self.name]

    def reload(self):
        found = self.bucket.objects.get(self.name)
        if found is None:
            raise gcp.NotFound(self.name)
        self._properties["generation"] = found[1]


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, tuple[str, int]] = {}
        self.seq = 0
        self.raises: Exception | None = None

    def blob(self, name):
        return FakeBlob(self, name)

    def fail_if_asked(self):
        if self.raises is not None:
            raise self.raises


class FakeStorageClient:
    def __init__(self):
        self._bucket = FakeBucket()

    def bucket(self, name):
        return self._bucket

    def list_blobs(self, bucket, prefix="", max_results=None):
        names = sorted(n for n in bucket.objects if n.startswith(prefix))
        return [FakeBlob(bucket, n) for n in names[:max_results]]


# ---------------------------------------------------------------------------
# A double for the tasks library
# ---------------------------------------------------------------------------


class FakeTasksClient:
    def __init__(self):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.raises: Exception | None = None

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, parent, task):
        if self.raises is not None:
            raise self.raises
        self.created.append(task)

        class Created:
            pass

        created = Created()
        created.name = f"{parent}/tasks/{len(self.created)}"
        return created

    def delete_task(self, name):
        if self.raises is not None:
            raise self.raises
        self.deleted.append(name)


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------

STORES = [
    pytest.param((store_mem, {}), id="simulated"),
    pytest.param((store_gcp, {"bucket": "b", "client": FakeStorageClient()}), id="adapter"),
]
if os.environ.get("GCS_BUCKET"):  # pragma: no cover - only with credentials
    from google.cloud import storage

    STORES.append(pytest.param(
        (store_gcp, {"bucket": os.environ["GCS_BUCKET"], "client": storage.Client(),
                     "prefix": f"contract/{os.getpid()}/"}), id="gcs"))

TIMERS = [
    pytest.param((timer_mem, {}), id="simulated"),
    pytest.param((timer_gcp, {"project": "p", "location": "europe-west1",
                              "queue": "execute", "base_url": "https://worker.example.com/",
                              "client": FakeTasksClient()}), id="adapter"),
]


@pytest.mark.parametrize("implementation", STORES)
def test_a_store_honours_its_contract(implementation):
    module, config = implementation
    assert store_spec.conformance(module, **config) == []


@pytest.mark.parametrize("implementation", TIMERS)
def test_a_timer_honours_its_contract(implementation):
    module, config = implementation
    assert timer_spec.conformance(module, **config) == []


@pytest.mark.parametrize("implementation", STORES)
def test_a_store_satisfies_the_interface_at_runtime(implementation):
    module, config = implementation
    m: store_spec.StoreM = module
    assert isinstance(m.Store(**config), store_spec.StoreP)


@pytest.mark.parametrize("implementation", TIMERS)
def test_a_timer_satisfies_the_interface_at_runtime(implementation):
    module, config = implementation
    m: timer_spec.TimerM = module
    assert isinstance(m.Timer(**config), timer_spec.TimerP)


def test_the_contracts_can_fail():
    """A contract nothing has ever failed is a wish. These two break one
    claim each and nothing else."""
    class BadStore(store_mem.Store):
        def put(self, key, body, *, if_match=None, if_absent=False):
            return super().put(key, body)  # ignores every precondition

    class BadTimer(timer_mem.Timer):
        def create(self, url, body, *, not_before=0):
            return "the-same-name-every-time"

    bad = store_spec.conformance(type("M", (), {"Store": BadStore}))
    assert bad and any("create" in v.msg for v in bad), bad
    bad = timer_spec.conformance(type("M", (), {"Timer": BadTimer}))
    assert bad and any("two creates" in v.msg for v in bad), bad


# ---------------------------------------------------------------------------
# What only the real store adapter can get wrong
# ---------------------------------------------------------------------------


def faked_store():
    return store_gcp.Store("bucket", client=FakeStorageClient())


def test_the_store_adapter_turns_throttling_into_unavailable():
    """A 429 says nothing about whether the write landed, which is a
    different thing from a refused precondition and must not be confused
    with one: one is re-decided, the other is retried."""
    s = faked_store()
    s.bucket.raises = gcp.TooManyRequests("slow down")
    for call in (lambda: s.get("wf/o"),
                 lambda: s.put("wf/o", "x"),
                 lambda: s.delete("wf/o")):
        with pytest.raises(Unavailable):
            call()


def test_the_store_adapter_creates_against_generation_zero():
    """The one line the whole design rests on. Zero is how GCS spells
    'only if nothing is there'."""
    seen = {}
    s = faked_store()
    real = s.bucket.blob

    def watched(name):
        blob = real(name)
        upload = blob.upload_from_string

        def record(data, content_type=None, if_generation_match=None):
            seen[name] = if_generation_match
            return upload(data, content_type=content_type,
                          if_generation_match=if_generation_match)

        blob.upload_from_string = record
        return blob

    s.bucket.blob = watched
    s.put("wf/o", "one", if_absent=True)
    assert seen["wf/o"] == 0
    s.put("wf/o", "two", if_match="1")
    assert seen["wf/o"] == 1
    s.put("t/deadline", "")
    assert seen["t/deadline"] is None, "an unconditional write carries no precondition"


# ---------------------------------------------------------------------------
# What only the real timer adapter can get wrong
# ---------------------------------------------------------------------------


def faked_timer(now=0, service_account=None):
    client = FakeTasksClient()
    t = timer_gcp.Timer("p", "europe-west1", "execute", "https://worker.example.com/",
                        service_account=service_account, client=client, now=lambda: now)
    return t, client


def test_a_dispatch_carries_no_schedule():
    t, client = faked_timer()
    t.create("execute", {"kind": "execute"})
    task = client.created[0]
    assert "schedule_time" not in task
    assert task["http_request"]["url"] == "https://worker.example.com/execute"
    assert json.loads(task["http_request"]["body"]) == {"kind": "execute"}


def test_a_deadline_carries_the_instant_it_is_for():
    t, client = faked_timer(now=1_000)
    t.create("sweep/o", {"origin": "o"}, not_before=61_000)
    assert client.created[0]["schedule_time"].timestamp() == pytest.approx(61.0)


def test_a_deadline_past_the_horizon_is_clamped_rather_than_refused():
    """Cloud Tasks will not schedule further out than thirty days. A promise
    with a longer deadline is clamped here, and the sweep it triggers finds
    nothing due and re-arms. A bug in this line makes a promise never time
    out, which is why it has a test of its own."""
    t, client = faked_timer(now=0)
    t.create("sweep/o", {}, not_before=10 * HORIZON_MS)
    assert client.created[0]["schedule_time"].timestamp() == pytest.approx(HORIZON_MS / 1_000)


def test_oidc_is_attached_when_a_service_account_is_named():
    t, client = faked_timer(service_account="worker@p.iam.gserviceaccount.com")
    t.create("execute", {})
    assert client.created[0]["http_request"]["oidc_token"] == {
        "service_account_email": "worker@p.iam.gserviceaccount.com"}


def test_without_a_service_account_nothing_is_signed():
    """Then the handler has to be unreachable from outside, and a
    deployment has to say which of the two it relies on."""
    t, client = faked_timer()
    t.create("execute", {})
    assert "oidc_token" not in client.created[0]["http_request"]


def test_throttling_a_create_is_unavailable():
    t, client = faked_timer()
    client.raises = gcp.ServiceUnavailable("try later")
    with pytest.raises(Unavailable):
        t.create("execute", {})


def test_a_worker_somewhere_else_is_reached_at_its_own_url():
    """A sweep is a path on this service. A worker is wherever it is, which
    on Cloud Run is a different service with a different hostname, and the
    address the kernel emitted already says so."""
    t, client = faked_timer()
    t.create("sweep/o", {})
    t.create("https://search-abc.a.run.app/execute", {})
    assert [task["http_request"]["url"] for task in client.created] == [
        "https://worker.example.com/sweep/o",
        "https://search-abc.a.run.app/execute",
    ]
