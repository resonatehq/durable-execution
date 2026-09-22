"""What is true of the simulated store and of no other.

The four operations are not tested here. They are `store.CLAIMS`, and
`test_conformance.py` runs them against every implementation, which is the
whole reason the contract was lifted out of this file: a claim written
beside one implementation is a claim the other is graded against by
accident.

What is left is what only a simulator has — the power cut — and it is not a
detail. Every crash-window test in the project rests on these two
behaviours being exactly what they say.
"""

from __future__ import annotations

import pytest

from spec import store as store_spec
import store_mem
from ports import Crash, Fault
from spec.store import PreconditionFailed
from store_mem import Store


def test_the_simulated_store_satisfies_the_contract():
    """Here as well as in `test_conformance.py`, because everything else in
    this project runs on this store: if it drifts, nothing else means
    anything, and the failure should surface next to it."""
    assert store_spec.conformance(store_mem) == []


def test_reads_never_fault():
    """So a test can see exactly what landed after the power went out. A
    store that could not be read after a crash would make every
    crash-window test a guess."""
    fault = Fault()
    s = Store(fault)
    s.put("wf/o", "one", if_absent=True)
    fault.crash_after(0)
    assert s.get("wf/o")[0] == "one"
    assert s.list("wf/", 10) == ["wf/o"]


def test_the_power_goes_out_before_the_write_by_default():
    fault = Fault()
    s = Store(fault)
    fault.crash_after(0)
    with pytest.raises(Crash):
        s.put("wf/o", "one", if_absent=True)
    assert s.get("wf/o") is None, "it wrote anyway"


def test_land_then_fail_is_the_window_nothing_can_close():
    """The commit landed and the answer was lost. The caller cannot tell it
    from a write that never happened, which is why every operation is
    idempotent and why this mode exists."""
    fault = Fault()
    s = Store(fault)
    s.land_then_fail = True
    fault.crash_after(0)
    with pytest.raises(Crash):
        s.put("wf/o", "one", if_absent=True)
    assert s.get("wf/o")[0] == "one", "the write did not land"


def test_a_precondition_is_checked_before_the_power_is_cut():
    """A refused write is refused whether or not the process was about to
    die: the two failures must not be able to masquerade as each other."""
    fault = Fault()
    s = Store(fault)
    s.put("wf/o", "one", if_absent=True)
    fault.crash_after(0)
    with pytest.raises(PreconditionFailed):
        s.put("wf/o", "two", if_absent=True)


def test_every_write_is_counted_once():
    fault = Fault()
    s = Store(fault)
    fault.crash_after(10)
    s.put("wf/o", "one", if_absent=True)
    s.delete("wf/o")
    assert fault.log == ["commit wf/o", "remove wf/o"]


def test_a_store_with_no_fault_never_faults():
    s = Store()
    s.put("wf/o", "one", if_absent=True)
    s.delete("wf/o")
    assert s.get("wf/o") is None
