"""The engine, held to its own specification.

`suite.conformance` is what a second implementation would be run through: a
Rust engine, one over a real bucket, a kernel written from the specification
rather than transcribed from it. Here it is run against the one engine there
is, which is what keeps the suite honest — a conformance suite nothing has
ever passed is a wish.
"""

from __future__ import annotations

from resonate import engine as engine_module
from resonate.engine import doc_key
from resonate.kernel import KernelCfg
from resonate.spec.engine import EngineM, EngineP
from resonate.types import Execute, PromiseCreate, PromiseGet, Reply, Value
# The engine's conformance suite, under the name the rest of this file uses.
from resonate.testing.conformance import engine as suite
from resonate.testing.conformance.engine import conformance
from resonate.testing.queue_mem import Queue
from resonate.testing.store_mem import Store


def test_the_engine_conforms():
    assert conformance(engine_module) == []


def test_the_engine_satisfies_the_protocol_at_runtime():
    e = engine_module.Engine(Store(), Queue())
    assert isinstance(e, EngineP)


def test_the_module_offers_a_constructor_under_the_agreed_name():
    m: EngineM = engine_module
    e = m.Engine(Store(), Queue(), KernelCfg())
    assert isinstance(e.process(PromiseCreate("o:a", 10, Value(), {}), 0), Reply)


def test_the_suite_rejects_an_engine_that_writes_on_a_read():
    """The suite has to be able to fail, or passing it means nothing. This
    engine breaks the write law and nothing else."""
    class Chatty(engine_module.Engine):
        def process(self, msg, now):
            reply = super().process(msg, now)
            if isinstance(msg, PromiseGet):
                key = doc_key(suite.ORIGIN)
                body, version = self.store.get(key)
                self.store.put(key, body, if_match=version)  # same bytes, at a cost
            return reply

    class Module:
        Engine = Chatty

    script = [(PromiseCreate("run", 1_000_000, Value(), {}), 0), (PromiseGet("run"), 1)]
    assert conformance(engine_module, script) == [], "the real engine reads for free"
    bad = conformance(Module(), script)
    assert bad and any("nothing changed, yet it wrote" in v.detail for v in bad)


def test_the_suite_rejects_an_engine_that_sends_before_it_commits():
    """The other half of the contract: a message must be a consequence of
    committed state, never of an intention."""
    class Eager(engine_module.Engine):
        def process(self, msg, now):
            from resonate.types import encode_message
            self.queue.create("http://w", encode_message(Execute("run", 0)))
            return super().process(msg, now)

    class Module:
        Engine = Eager

    bad = conformance(Module())
    assert bad and any("before the commit" in v.detail or "without a commit" in v.detail
                       for v in bad)


def test_the_standard_script_exercises_what_it_claims_to():
    """A conformance script that never suspends a task, never expires a
    lease and never settles a timer grades nothing."""
    from resonate.engine import decode
    from resonate.testing.queue_mem import Queue
    from resonate.testing.store_mem import Store
    store = Store()
    e = engine_module.Engine(store, Queue(), suite.CFG)
    seen = set()
    for msg, now in suite.STANDARD_SCRIPT:
        e.process(msg, now)
        raw = store.get(doc_key(suite.ORIGIN))[0]
        for o in decode(raw).objects:
            if o.task is not None:
                seen.add(o.task.state)
            seen.add(o.promise.state)
    assert {"pending", "acquired", "suspended", "fulfilled", "resolved"} <= seen, seen


def test_the_engine_conforms_over_a_store_it_was_handed():
    """The same engine, the same script, the same catalogue, over a store
    the suite was given rather than the one it defaults to. Any module that
    passes `store.conformance` can be dropped in here — including
    `store_gcp` against a real bucket."""
    from resonate.testing import store_mem
    assert conformance(engine_module, store=store_mem.Store()) == []
