"""The module specs, checked by something that is not us.

`spec.engine.EngineM`, `spec.store.StoreM` and `spec.queue.QueueM` are claims in the type
system, and only the type system can say whether they hold. `Engine: EngineC`
would pass every other test and still be a form mypy rejects, because a
protocol's mutable attribute is invariant and no class object is *exactly* a
callback protocol. A type checker is an oracle: it is not ours, it does not
know what we meant, and it says no for reasons of its own.

The claims are below, in a block that only a type checker reads. The test
runs mypy over this file and fails on anything it says about it. Errors in
other files are silenced rather than followed, because this file is about
the specs, not about annotating a codebase that has no annotations to
speak of.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

#: Where the code is. The tests live one level down, in `test/`.
ROOT = Path(__file__).parent.parent

if TYPE_CHECKING:
    from resonate import engine
    from resonate import queue_gcp
    from resonate.testing import queue_mem
    from resonate import store_gcp
    from resonate.testing import store_mem
    from resonate.testing.spec import engine as spec
    from resonate.testing.spec import queue as queue_spec
    from resonate.testing.spec import store as store_spec

    # A module offers what its spec says it offers.
    an_engine: spec.EngineM = engine
    a_simulated_store: store_spec.StoreM = store_mem
    a_real_store: store_spec.StoreM = store_gcp
    a_simulated_queue: queue_spec.QueueM = queue_mem
    a_real_queue: queue_spec.QueueM = queue_gcp

    # And the engine's constructor really is pinned to those two ports,
    # which is the claim `StoreC` and `QueueC` deliberately do not make.
    a_constructor: spec.EngineC = engine.Engine


def test_the_module_specs_hold():
    if shutil.which("mypy") is None:  # pragma: no cover - mypy is optional
        pytest.skip("mypy is not installed")
    done = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary",
         "--follow-imports=silent", "test/test_types.py"],
        cwd=ROOT, capture_output=True, text=True,
        env={"MYPYPATH": str(ROOT), "PATH": ""})
    assert done.stdout == "" and done.returncode == 0, done.stdout or done.stderr
