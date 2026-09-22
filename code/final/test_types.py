"""The module specs, checked by something that is not us.

`spec.engine.EngineM`, `spec.store.StoreM` and `spec.queue.QueueM` are claims in the type
system, and nothing in this project had ever asked the type system whether
they hold. Every test passed with `Engine: EngineC` — a form mypy rejects,
because a protocol's mutable attribute is invariant and no class object is
*exactly* a callback protocol. The tests could not have caught it. A type
checker is an oracle in the same sense the line schema is: it is not ours,
it does not know what we meant, and it says no for reasons of its own.

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

HERE = Path(__file__).parent

if TYPE_CHECKING:
    import engine
    import queue_gcp
    import queue_mem
    import store_gcp
    import store_mem
    from spec import engine as spec
    from spec import queue as queue_spec
    from spec import store as store_spec

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
         "--follow-imports=silent", "test_types.py"],
        cwd=HERE, capture_output=True, text=True, env={"MYPYPATH": str(HERE), "PATH": ""})
    assert done.stdout == "" and done.returncode == 0, done.stdout or done.stderr
