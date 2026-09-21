"""The bucket's four operations, and the adapter that narrows them.

Every claim here is one a real store makes too, which is the point: an
adapter to Google Cloud Storage has to satisfy these, and if it cannot then
this interface is wrong rather than the store.
"""

from __future__ import annotations

import pytest

from blob import BlobStore, MemoryBlob, PreconditionFailed


def test_getting_what_is_not_there_is_not_an_error():
    assert MemoryBlob().get("wf/o") is None


def test_a_create_wins_once():
    b = MemoryBlob()
    v = b.put("wf/o", "one", if_absent=True)
    assert b.get("wf/o") == ("one", v)
    with pytest.raises(PreconditionFailed):
        b.put("wf/o", "two", if_absent=True)
    assert b.get("wf/o")[0] == "one", "the loser wrote nothing"


def test_a_replacement_needs_the_version_it_read():
    b = MemoryBlob()
    first = b.put("wf/o", "one", if_absent=True)
    second = b.put("wf/o", "two", if_match=first)
    assert second != first
    with pytest.raises(PreconditionFailed):
        b.put("wf/o", "three", if_match=first)
    assert b.get("wf/o")[0] == "two"


def test_a_replacement_of_nothing_is_refused():
    with pytest.raises(PreconditionFailed):
        MemoryBlob().put("wf/o", "one", if_match="v1")


def test_the_two_conditions_are_exclusive():
    with pytest.raises(ValueError):
        MemoryBlob().put("wf/o", "one", if_match="v1", if_absent=True)


def test_an_unconditional_write_is_for_keys_that_carry_their_own_value():
    """A deadline named by its instant: writing it twice writes the same
    fact twice, so there is nothing to condition on."""
    b = MemoryBlob()
    b.put("t/0000000010_o", "")
    b.put("t/0000000010_o", "")
    assert b.get("t/0000000010_o")[0] == ""


def test_removing_what_is_not_there_succeeds():
    MemoryBlob().delete("wf/o")  # a collector that crashed and ran again


def test_listing_is_lexicographic_ascending_and_capped():
    """Load-bearing: a deadline zero-padded into its key sorts into time
    order, so the nearest deadlines are a capped listing."""
    b = MemoryBlob()
    for at in (300, 10, 2_000, 45):
        b.put(f"t/{at:020d}_o", "")
    b.put("wf/o", "{}")
    assert b.list("t/", 10) == [f"t/{at:020d}_o" for at in (10, 45, 300, 2_000)]
    assert b.list("t/", 2) == [f"t/{at:020d}_o" for at in (10, 45)]
    assert b.list("wf/", 10) == ["wf/o"], "a prefix is a prefix"


# --- the adapter -----------------------------------------------------------


def test_the_adapter_reports_a_missing_document_as_absent():
    s = BlobStore(MemoryBlob())
    assert s.load("wf/o") == (None, BlobStore.ABSENT)


def test_the_adapter_creates_against_absent_and_replaces_against_a_version():
    s = BlobStore(MemoryBlob())
    v = s.commit("wf/o", b"one", BlobStore.ABSENT)
    assert s.load("wf/o") == (b"one", v)
    v2 = s.commit("wf/o", b"two", v)
    assert s.load("wf/o") == (b"two", v2)


def test_the_adapter_refuses_a_create_over_something_that_exists():
    s = BlobStore(MemoryBlob())
    s.commit("wf/o", b"one", BlobStore.ABSENT)
    with pytest.raises(PreconditionFailed):
        s.commit("wf/o", b"two", BlobStore.ABSENT)


def test_the_adapter_refuses_a_stale_replacement():
    s = BlobStore(MemoryBlob())
    first = s.commit("wf/o", b"one", BlobStore.ABSENT)
    s.commit("wf/o", b"two", first)
    with pytest.raises(PreconditionFailed):
        s.commit("wf/o", b"three", first)
