"""What the three interfaces are: one module each, three layers each.

    spec/engine.py   EngineP  EngineC  EngineM   ->  engine.py
    spec/store.py    StoreP   StoreC   StoreM    ->  testing/store_mem.py   store_gcp.py
    spec/queue.py    QueueP   QueueC   QueueM    ->  testing/queue_mem.py   queue_gcp.py

The layers are three things worth naming separately: the thing once it
exists, how one is made, and a module that offers one. Nothing here imports
anything that is not an interface -- these are types and nothing else, which
is what lets `engine.py` be written against `StoreP` without reaching into
the test harness for it.

What an implementation has to *do* to be one of these is next door, in
`testing/conformance/`, one suite per interface:

    python -m resonate.testing.conformance.check

A contract lives with the interface rather than with an implementation
because a suite that shipped with the simulator would be grading the bucket
against a rival instead of against a contract. It lives under `testing/`
rather than here because running it is checking, and nothing in production
imports it.
"""
