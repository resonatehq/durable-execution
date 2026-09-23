"""The program from the repository's README, as something deployable.

This is not part of the engine. It is the example agent every other part
of the project is measured against, in a module a container can import,
because a worker with no registered functions can serve the protocol and
still answer `KeyError` to the first dispatch that arrives -- which is
exactly what the first Cloud Run deployment did.

`test_e2e.py` defines the same three functions for its own use. They are
not shared, on purpose: the test owns a counter that proves nothing is
paid for twice, and production has no business carrying that.
"""

from __future__ import annotations

from sdk import gather, resonate, sleep


@resonate
async def agent(prompt: str):
    """A model call. Async, because that is what a model call is."""
    if prompt.startswith("Plan"):
        return ["durable execution", "workflow recovery", "sagas"]
    return {"report": prompt}


@resonate
def search(query: str):
    """A leaf with nothing to await. It does not have to pretend."""
    return f"finding about {query}"


@resonate
async def research(question: str):
    # Plan the searches
    queries = await agent(f"Plan the searches for: {question}")

    # Fan out the searches
    results = await gather(search.rpc(q) for q in queries)

    # Synthesize the results
    return await agent(f"Write a cited report. {question}: {results}")


@resonate
async def nap(ms: int):
    """A durable sleep, as small as one can be and still be worth deploying.

    Nothing runs while this waits. The worker suspends, the container is free
    to go away, and what brings the run back is a deadline in the document
    that the shell handed to the queue as a schedule. On a laptop that is a
    clock a test moves; on Cloud Run it is Cloud Tasks deciding the time has
    come, which is the only version of the claim that is not a simulation.
    """
    await sleep(ms)
    return {"slept": ms}
