"""What a user writes. The whole of it.

This is not part of the engine. It is the file you would write to deploy
something of your own, and it is the file `test_http.py` serves, so the
example and the thing under test are the same file rather than two that
drift.

The name is Google's: the Python buildpack looks for `main.py` at the root
of what you deploy and fails with `MissingSourceException` otherwise. The
entry point is `handler`, built by `serve()` on the last line --
`--function handler` looks for a module-level function by that name.

    gcloud run deploy my-agent --source . --function handler \
      --set-env-vars BUCKET=...,PROJECT=...,LOCATION=...,QUEUE=...,BASE_URL=...

Beside it you need a `requirements.txt` naming this package, and that is
the end of the list.

Nothing here is versioned, which is the right state for code that has
never been deployed. The day you change one of these bodies while runs of
it are still in flight -- insert an `await`, reorder two -- the old runs
replay by position into the new body and read the wrong answers back. That
is when you write `@resonate(version=1)` above the new one and leave the
old one where it is, so runs finish on the body they started on. Changing
what a call *does* without changing which calls are made needs no version.
"""

from __future__ import annotations

from resonate import gather, resonate, serve, sleep


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

    Nothing runs while this waits. The worker suspends, the container is
    free to go away, and what brings the run back is a deadline in the
    document that the engine handed to the queue as a scheduled `timeout`
    message. On a laptop that is a clock a test moves; on Cloud Run it is
    Cloud Tasks deciding the time has come, which is the only version of
    the claim that is not a simulation.
    """
    await sleep(ms)
    return {"slept": ms}


handler = serve()
