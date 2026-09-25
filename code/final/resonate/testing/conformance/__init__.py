"""What it takes to be one of the three interfaces, one suite per interface.

`spec/` says what each interface is; this says what an implementation has
to do to be one, and reports the same way for all three:

    conformance/engine.py   a script, and every state it commits
    conformance/store.py    eleven claims
    conformance/queue.py    eight claims

    python -m resonate.testing.conformance.check

runs all of them, top to bottom, against every implementation, and says
what is implemented and what holds. What it cannot answer without
credentials it reports as a skip rather than a pass.

These live under `testing/` because running them is checking: nothing in
production imports this package, and `test_deploy.py` is what notices if
that stops being true.
"""
