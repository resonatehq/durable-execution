"""The simulated world, as a module, so two importers share one copy.

`functions_framework.create_app("handler", "app.py")` loads `app.py` as a
module object of its own, so a test cannot reach into the service it built:
setting `app.SERVICE` from outside sets it on a different module. Anything
the test and the service must both hold has to live somewhere they both
import, which is here.

That is the mechanical reason. The useful one is that `SIMULATED=1` makes
the whole service runnable on a laptop:

    SIMULATED=1 functions-framework --target=handler

and then `curl localhost:8080/` speaks the protocol, with the same engine,
the same kernel and the same codec as production. Only the two ports are
different, and both are held to the same contracts as the real ones.
"""

from __future__ import annotations

from queue_mem import Queue
from runtime import Clock
from store_mem import Store

#: One store, one queue, one clock, for the life of the process.
STORE = Store()
QUEUE = Queue()
CLOCK = Clock()


def reset() -> None:
    """Forget everything. For a test that wants a fresh world without a
    fresh process."""
    STORE.objects.clear()
    QUEUE.entries.clear()
    QUEUE.created.clear()
    QUEUE.dropped.clear()
    QUEUE.delivered = 0
    CLOCK.now = 0
