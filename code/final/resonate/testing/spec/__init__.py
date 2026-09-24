"""What the three interfaces are, and what it takes to be one.

One module per interface, each carrying the same three layers and the
contract that goes with them (`StoreP` and `QueueP` are defined in
`ports.py` and re-used here):

    spec/engine.py   EngineP  EngineC  EngineM   ->  engine.py
    spec/store.py    StoreP   StoreC   StoreM    ->  testing/store_mem.py   store_gcp.py
    spec/queue.py    QueueP   QueueC   QueueM    ->  testing/queue_mem.py   queue_gcp.py

A contract lives beside the interface rather than beside an implementation
because a suite that shipped with the simulator would be grading the bucket
against a rival instead of against a contract.

    python -m resonate.testing.spec.check

runs all of them, top to bottom, and says what is implemented and what
holds.
"""
