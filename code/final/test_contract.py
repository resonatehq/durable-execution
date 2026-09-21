"""One contract, every implementation.

The simulated bucket and the real one are the same thing to everything
above them, and the only way to keep that true is to write the claims once
and run them against both. The simulated one runs here always. The real one
runs when `GCS_BUCKET` names a bucket this machine can reach, and is
skipped otherwise, so a green suite never implies a live one.

Between those two there is a third: the real adapter over a double that
raises the library's own exceptions. It cannot say whether Google Cloud
Storage behaves as documented, but it does say whether our adapter calls
what it meant to, spells the preconditions the library's way, and turns
each failure into the right one of ours. Those are the mistakes an adapter
actually makes.
"""

from __future__ import annotations

import json
import os

import pytest

#: The adapters' own tests, and only those, need Google's libraries. Skipping
#: rather than failing keeps `pip install -r requirements-dev.txt` honest about
#: what the kernel and the simulators actually depend on, which is nothing.
gcp = pytest.importorskip("google.api_core.exceptions")

from blob import MemoryBlob, PreconditionFailed
from cloudtasks import HORIZON_MS, CloudTasksQueue
from gcs import ABSENT, CONTENT_TYPE, GcsBlob
from ports import Unavailable
from tasks import MemoryQueue

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
# The bucket contract
# ---------------------------------------------------------------------------


def memory():
    return MemoryBlob()


def faked():
    return GcsBlob("bucket", client=FakeStorageClient())


def live():  # pragma: no cover - only with credentials
    from google.cloud import storage

    name = os.environ["GCS_BUCKET"]
    return GcsBlob(name, client=storage.Client(), prefix=f"contract/{os.getpid()}/")


IMPLEMENTATIONS = [pytest.param(memory, id="simulated"), pytest.param(faked, id="adapter")]
if os.environ.get("GCS_BUCKET"):  # pragma: no cover
    IMPLEMENTATIONS.append(pytest.param(live, id="gcs"))


@pytest.fixture(params=IMPLEMENTATIONS)
def blob(request):
    return request.param()


def test_getting_what_is_not_there_is_not_an_error(blob):
    assert blob.get("wf/o") is None


def test_a_create_wins_once(blob):
    v = blob.put("wf/o", "one", if_absent=True)
    assert blob.get("wf/o") == ("one", v)
    with pytest.raises(PreconditionFailed):
        blob.put("wf/o", "two", if_absent=True)
    assert blob.get("wf/o")[0] == "one"


def test_a_replacement_needs_the_version_it_read(blob):
    first = blob.put("wf/o", "one", if_absent=True)
    second = blob.put("wf/o", "two", if_match=first)
    assert second != first
    with pytest.raises(PreconditionFailed):
        blob.put("wf/o", "three", if_match=first)
    assert blob.get("wf/o")[0] == "two"


def test_a_replacement_of_nothing_is_refused(blob):
    with pytest.raises(PreconditionFailed):
        blob.put("wf/o", "one", if_match="1")


def test_the_two_conditions_are_exclusive(blob):
    with pytest.raises(ValueError):
        blob.put("wf/o", "one", if_match="1", if_absent=True)


def test_removing_what_is_not_there_succeeds(blob):
    blob.delete("wf/gone")


def test_listing_is_lexicographic_ascending_and_capped(blob):
    for at in (300, 10, 2_000, 45):
        blob.put(f"t/{at:020d}_o", "")
    blob.put("wf/o", "{}")
    want = [f"t/{at:020d}_o" for at in (10, 45, 300, 2_000)]
    assert blob.list("t/", 10) == want
    assert blob.list("t/", 2) == want[:2]
    assert blob.list("wf/", 10) == ["wf/o"]


def test_a_document_survives_the_round_trip_unchanged(blob):
    body = '{"t":"h","v":1}\n{"t":"o","id":"o:\\u00e9"}'
    blob.put("wf/o", body, if_absent=True)
    assert blob.get("wf/o")[0] == body


# --- what only the real adapter can get wrong ------------------------------


def test_the_adapter_turns_throttling_into_unavailable():
    """A 429 says nothing about whether the write landed, which is a
    different thing from a refused precondition and must not be confused
    with one: one is re-decided, the other is retried."""
    b = faked()
    b.bucket.raises = gcp.TooManyRequests("slow down")
    for call in (lambda: b.get("wf/o"),
                 lambda: b.put("wf/o", "x"),
                 lambda: b.delete("wf/o")):
        with pytest.raises(Unavailable):
            call()


def test_the_adapter_creates_against_generation_zero():
    """The one line the whole design rests on. Zero is how GCS spells
    'only if nothing is there'."""
    seen = {}
    b = faked()
    real = b.bucket.blob

    def watched(name):
        blob = real(name)
        upload = blob.upload_from_string

        def record(data, content_type=None, if_generation_match=None):
            seen[name] = if_generation_match
            return upload(data, content_type=content_type,
                          if_generation_match=if_generation_match)

        blob.upload_from_string = record
        return blob

    b.bucket.blob = watched
    b.put("wf/o", "one", if_absent=True)
    assert seen["wf/o"] == 0
    b.put("wf/o", "two", if_match="1")
    assert seen["wf/o"] == 1
    b.put("t/deadline", "")
    assert seen["t/deadline"] is None, "an unconditional write carries no precondition"


def test_the_adapter_reports_absence_the_way_the_engine_expects():
    assert faked().get("wf/o") is None and ABSENT == ""


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
        name = f"{parent}/tasks/{len(self.created)}"

        class Created:
            pass

        created = Created()
        created.name = name
        return created

    def delete_task(self, name):
        if self.raises is not None:
            raise self.raises
        self.deleted.append(name)


def queue(now=0, service_account=None):
    client = FakeTasksClient()
    q = CloudTasksQueue("p", "europe-west1", "execute", "https://worker.example.com/",
                        service_account=service_account, client=client, now=lambda: now)
    return q, client


def test_a_dispatch_carries_no_schedule():
    q, client = queue()
    q.create("execute", {"kind": "execute"})
    task = client.created[0]
    assert "schedule_time" not in task
    assert task["http_request"]["url"] == "https://worker.example.com/execute"
    assert json.loads(task["http_request"]["body"]) == {"kind": "execute"}


def test_a_deadline_carries_the_instant_it_is_for():
    q, client = queue(now=1_000)
    q.create("sweep/o", {"origin": "o"}, not_before=61_000)
    at = client.created[0]["schedule_time"]
    assert at.timestamp() == pytest.approx(61.0)


def test_a_deadline_past_the_horizon_is_clamped_rather_than_refused():
    """Cloud Tasks will not schedule further out than thirty days. A promise
    with a longer deadline is clamped here, and the sweep it triggers finds
    nothing due and re-arms. A bug in this line makes a promise never time
    out, which is why it has a test of its own."""
    q, client = queue(now=0)
    q.create("sweep/o", {}, not_before=10 * HORIZON_MS)
    at = client.created[0]["schedule_time"]
    assert at.timestamp() == pytest.approx(HORIZON_MS / 1_000)


def test_oidc_is_attached_when_a_service_account_is_named():
    q, client = queue(service_account="worker@p.iam.gserviceaccount.com")
    q.create("execute", {})
    assert client.created[0]["http_request"]["oidc_token"] == {
        "service_account_email": "worker@p.iam.gserviceaccount.com"}


def test_without_a_service_account_nothing_is_signed():
    """Then the handler has to be unreachable from outside, and a
    deployment has to say which of the two it relies on."""
    q, client = queue()
    q.create("execute", {})
    assert "oidc_token" not in client.created[0]["http_request"]


def test_cancelling_what_is_gone_succeeds():
    q, client = queue()
    client.raises = gcp.NotFound("already fired")
    q.delete("projects/p/locations/l/queues/q/tasks/1")


def test_throttling_a_create_is_unavailable():
    q, client = queue()
    client.raises = gcp.ServiceUnavailable("try later")
    with pytest.raises(Unavailable):
        q.create("execute", {})


def test_the_simulated_queue_and_the_real_one_agree_on_the_protocol():
    """Not on behaviour, which only a live run can say, but on the shape:
    both create by url and body with an optional not-before, and both
    delete by the name the service gave back."""
    from tasks import Queue

    for q in (MemoryQueue(), queue()[0]):
        assert isinstance(q, Queue)
        name = q.create("sweep/o", {"origin": "o"}, not_before=10)
        assert isinstance(name, str) and name
        q.delete(name)


def test_the_simulated_bucket_and_the_real_one_agree_on_the_protocol():
    from blob import Blob

    assert isinstance(MemoryBlob(), Blob)
    assert isinstance(faked(), Blob)


def test_a_worker_somewhere_else_is_reached_at_its_own_url():
    """A sweep is a path on this service. A worker is wherever it is, which
    on Cloud Run is a different service with a different hostname, and the
    address the kernel emitted already says so."""
    q, client = queue()
    q.create("sweep/o", {})
    q.create("https://search-abc.a.run.app/execute", {})
    assert [t["http_request"]["url"] for t in client.created] == [
        "https://worker.example.com/sweep/o",
        "https://search-abc.a.run.app/execute",
    ]
