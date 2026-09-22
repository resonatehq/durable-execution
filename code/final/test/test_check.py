"""`python -m spec.check` says what it sees, and can say no.

A checker that has never failed is a wish, so this breaks one thing of
each kind and confirms the checker notices: a module that does not offer
what its spec names, a class missing an operation, and an implementation
whose behaviour does not hold.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import queue_mem
import store_mem
from spec import queue as queue_spec
from spec import store as store_spec

#: Where the code is. The tests live one level down, in `test/`.
ROOT = Path(__file__).parent.parent


def run() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "spec.check"],
                          cwd=ROOT, capture_output=True, text=True)


def test_everything_checks_out():
    done = run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "every interface is implemented" in done.stdout
    # It must actually have looked at all five, not just said so.
    for name in ("engine", "store_mem", "store_gcp", "queue_mem", "queue_gcp"):
        assert name in done.stdout, name


def test_it_admits_what_it_could_not_check():
    """A skip is not a pass. Without credentials the real store's
    behaviour is unknown, and the output has to say so."""
    done = run()
    assert "skip" in done.stdout and "GCS_BUCKET" in done.stdout


def test_a_module_that_offers_nothing_is_caught():
    empty = type("M", (), {})
    assert [n for n in vars(store_spec.StoreM) if not n.startswith("_")] == ["Store"]
    assert not hasattr(empty, "Store"), "the premise of the check"


def test_a_missing_operation_is_caught():
    class Half(store_mem.Store):
        delete = None

    del Half.delete
    missing = [n for n in vars(store_spec.StoreP)
               if not n.startswith("_") and not hasattr(Half, n)]
    assert missing == [], "subclassing keeps it; the real case is a rename"

    renamed = type("Renamed", (), {"get": None, "put": None, "list": None})
    missing = [n for n in vars(store_spec.StoreP)
               if not n.startswith("_") and not hasattr(renamed, n)]
    assert missing == ["delete"]


@pytest.mark.parametrize("spec_module, implementation, broken", [
    (store_spec, store_mem, "Store"),
    (queue_spec, queue_mem, "Queue"),
])
def test_behaviour_that_does_not_hold_is_caught(spec_module, implementation, broken):
    """The third question, the one shape cannot answer."""
    class Liar(getattr(implementation, broken)):
        def create(self, url, body, *, not_before=0):
            return "the-same-name-every-time"

        def put(self, key, body, *, if_match=None, if_absent=False):
            return super().put(key, body)  # ignores every precondition

    assert spec_module.conformance(type("M", (), {broken: Liar})) != []
