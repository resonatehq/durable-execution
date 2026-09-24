"""Two functions cannot share a name, and one function can have two bodies.

Those are the same rule seen from both sides. A promise records the call it
stands for as `{"f": "research"}`, and a worker looks the code up by that,
so the name is not a label -- it is the protocol's identifier. Two
unrelated functions answering to it means a dispatch created for one runs
the other, silently, and the only symptom is a wrong answer.

That was the behaviour until this file existed: `@resonate` wrote to a
dict and the last module imported won. It was found by moving the engine
into a package, which made `main.py` and `test_sleep.py` both define `nap`
-- the suite became order-dependent and only one of the two noticed.

The other side of it is that a durable function's body cannot be changed
freely while runs of it are in flight. A run replays from the top and
reads its previous calls back *by position*, so inserting a call or
reordering two moves every position after it, and an in-flight run
resuming into the new body reads an answer that belongs to a different
call. `@resonate(version=1)` deploys the new body beside the old one, and
the run finishes on the body it started on. That is the test at the bottom
of this file, and it is the reason versions exist at all.
"""

from __future__ import annotations

import json
import sys
from importlib import util

import pytest

from resonate.codec import decode, doc_key
from resonate.engine import Engine
from resonate.kernel import KernelCfg
from resonate.testing.queue_mem import Queue
from resonate.testing.sim import Clock, Runtime
from resonate.worker import Worker
from resonate.sdk import (
    REGISTRY, DuplicateFunction, UnknownFunction, call_param, called, gather,
    lookup, resonate,
)
from resonate.testing.store_mem import Store

CFG = KernelCfg(retry_timeout=30_000)
WORKER = "worker://w"


@pytest.fixture(autouse=True)
def clean_registry():
    """`@resonate` writes to one global dict. A test that left something in
    it would be handing the next test a collision it did not cause."""
    before = dict(REGISTRY)
    yield
    REGISTRY.clear()
    REGISTRY.update(before)


def module_from(tmp_path, name: str, source: str):
    """A module in a file of its own, imported. Two of these are two files,
    which is the distinction the collision rule turns on."""
    path = tmp_path / f"{name}.py"
    path.write_text("from resonate import resonate, gather\n\n" + source)
    spec = util.spec_from_file_location(name, path)
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


# --- one name, one function ------------------------------------------------


def test_two_files_cannot_claim_one_name(tmp_path):
    """The bug this file was written for. `billing.py` and `orders.py` each
    define `process`; one of them silently wins."""
    module_from(tmp_path, "billing", "@resonate\ndef process(x):\n    return 'billed'\n")
    with pytest.raises(DuplicateFunction) as caught:
        module_from(tmp_path, "orders", "@resonate\ndef process(x):\n    return 'ordered'\n")

    said = str(caught.value)
    assert "billing.py" in said and "orders.py" in said, said
    assert "process" in said
    assert REGISTRY[("process", 0)].fn(None) == "billed", "the first one was replaced"


def test_the_same_file_loaded_twice_is_not_a_collision(tmp_path):
    """`functions_framework.create_app` loads `main.py` as a module object
    of its own, so a file that is also imported normally runs its
    decorators twice. That is one function, twice, and refusing it would
    refuse every deployment."""
    src = "@resonate\ndef only(x):\n    return x\n"
    (tmp_path / "twice.py").write_text("from resonate import resonate\n\n" + src)
    for _ in range(2):
        spec = util.spec_from_file_location("twice", tmp_path / "twice.py")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)      # no raise
    assert ("only", 0) in REGISTRY


def test_two_versions_of_one_name_are_not_a_collision(tmp_path):
    """What the rule is for: saying *these are the same function* rather
    than being told two different ones share a name."""
    module_from(tmp_path, "v0mod", "@resonate\ndef step(x):\n    return 'old'\n")
    module_from(tmp_path, "v1mod",
                "@resonate(version=1)\ndef step(x):\n    return 'new'\n")
    assert {("step", 0), ("step", 1)} <= set(REGISTRY)
    assert REGISTRY[("step", 0)].fn(None) == "old"
    assert REGISTRY[("step", 1)].fn(None) == "new"


def test_the_same_version_twice_is_still_a_collision(tmp_path):
    module_from(tmp_path, "a1", "@resonate(version=3)\ndef step(x):\n    return 1\n")
    with pytest.raises(DuplicateFunction):
        module_from(tmp_path, "b1", "@resonate(version=3)\ndef step(x):\n    return 2\n")


@pytest.mark.parametrize("bad", [-1, 1.5, "1", True, None])
def test_a_version_is_a_non_negative_integer(bad):
    with pytest.raises((ValueError, TypeError)):
        resonate(version=bad)(lambda x: x)


# --- what a version costs when you do not use one --------------------------


def test_an_unversioned_call_writes_what_it_always_wrote():
    """Version zero adds no key. Documents written before versions existed
    still decode, and the reviewed trace does not move for a feature nobody
    in that project is using."""
    @resonate
    def plain(x):
        return x

    assert json.loads(call_param(plain, ("q",)).data) == {"f": "plain", "a": ["q"]}


def test_a_versioned_call_says_so():
    @resonate(version=7)
    def marked(x):
        return x

    assert json.loads(call_param(marked, ("q",)).data) == {
        "f": "marked", "a": ["q"], "v": 7}


def test_a_parameter_with_no_version_is_version_zero():
    """The compatibility rule, stated once. Everything written before this
    feature is version zero, which is what it was."""
    assert called({"f": "old", "a": [1]}) == ("old", 0, [1])
    assert called({"f": "new", "a": [1], "v": 2}) == ("new", 2, [1])


def test_a_dispatch_for_a_version_this_worker_lacks_says_what_it_has():
    """Retire a version while runs created under it are in flight and this
    is what the worker says. The useful question is which versions this
    container carries, so the answer is that."""
    @resonate(version=1)
    def only_one(x):
        return x

    with pytest.raises(UnknownFunction) as caught:
        lookup("only_one", 2)
    assert "only_one@1" in str(caught.value), caught.value

    with pytest.raises(UnknownFunction) as caught:
        lookup("never_deployed")
    assert "no function by that name" in str(caught.value)


# --- the reason versions exist ---------------------------------------------


RAN: list[str] = []


def two_generations():
    """One function, changed the way that breaks a run in flight.

    Version 1 inserts a durable call *before* the one version 0 made. Every
    position after it shifts by one, so a run that recorded `:1` as its
    lookup would replay into version 1 and read the audit's answer instead.
    """
    @resonate
    def versioned_leaf(x: str):
        RAN.append(f"versioned_leaf:{x}")
        return f"looked up {x}"

    @resonate
    async def job(x: str):
        RAN.append("job@0")
        return {"v": 0, "got": await versioned_leaf.rpc(x)}

    @resonate(version=1)
    async def job_v1(x: str):
        RAN.append("job@1")
        await versioned_leaf.rpc(f"audit {x}")          # the inserted call
        return {"v": 1, "got": await versioned_leaf.rpc(x)}

    return job, job_v1, versioned_leaf


def world(*functions):
    RAN.clear()
    store, queue, clock = Store(), Queue(), Clock()
    engine = Engine(store, queue, CFG)
    rt = Runtime(engine, queue, clock)
    rt.serve(WORKER, Worker(engine, clock, "w-1"), *functions)
    return rt, store


def document(store, origin):
    found = store.get(doc_key(origin))
    return decode(found[0].encode()) if found else None


def test_a_run_finishes_on_the_body_it_started_on():
    """The claim, end to end, with both generations deployed at once.

    The run is started under version 0 and never sees version 1, although
    version 1 is registered, routed and would answer to the same name. It
    suspends on its `rpc`, comes back in a later delivery, and resumes into
    the body that recorded its positions.
    """
    job, job_v1, versioned_leaf = two_generations()
    rt, store = world(job, job_v1, versioned_leaf)

    rt.start("job.1", job, "widgets")
    rt.drain()

    root = document(store, "job.1").get("job.1").promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == {"v": 0, "got": "looked up widgets"}
    assert "job@1" not in RAN, RAN
    assert [o.id for o in document(store, "job.1").objects] == ["job.1", "job.1:1"], \
        "version 1 would have made two child calls, not one"


def test_a_new_run_takes_the_new_body():
    """Both deployed, and the caller decides which -- by naming it, which is
    what `@resonate(version=1)` gives them a way to do."""
    job, job_v1, versioned_leaf = two_generations()
    rt, store = world(job, job_v1, versioned_leaf)

    rt.start("job.2", job_v1, "widgets")
    rt.drain()

    root = document(store, "job.2").get("job.2").promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == {"v": 1, "got": "looked up widgets"}
    assert [o.id for o in document(store, "job.2").objects] == [
        "job.2", "job.2:1", "job.2:2"], "the inserted call is missing"


def test_the_version_travels_with_the_dispatch():
    """Not with the worker, and not with the deployment. A task created
    three days ago still names the body it was written against, which is
    the only thing that makes the test above survive a real deploy."""
    job, job_v1, versioned_leaf = two_generations()
    rt, store = world(job, job_v1, versioned_leaf)
    rt.start("job.3", job_v1, "widgets")

    body = next(e.body for e in rt.queue.entries.values()
                if e.body and e.body.get("kind") == "execute")
    assert body["taskId"] == "job.3"

    param = json.loads(document(store, "job.3").get("job.3").promise.param.data)
    assert param["v"] == 1, param
    assert called(param)[:2] == ("job_v1", 1)
