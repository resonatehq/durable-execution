"""What a user writes. The whole of it.

This is not part of the engine. It is the file you would write to deploy
something of your own, and it is the file this project deploys, so the
example and the thing under test are the same file rather than two that
drift.

The name is Google's: the Python buildpack looks for `main.py` at the root
of what you deploy and fails with `MissingSourceException` otherwise. The
entry point is `handler`, which is why it is imported and never called --
`--function handler` looks for a module-level name, and the import is what
puts one there. A linter that strips unused imports will delete your
service's entry point, which is what the `noqa` is for.

    gcloud run deploy my-agent --source . --function handler \
      --set-env-vars BUCKET=...,QUEUE=...

Beside it you need a `requirements.txt` naming this package, and that is
the end of the list.
"""

from __future__ import annotations

from resonate import gather, handler, resonate, sleep  # noqa: F401


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
    document that the shell handed to the queue as a schedule. On a laptop
    that is a clock a test moves; on Cloud Run it is Cloud Tasks deciding
    the time has come, which is the only version of the claim that is not a
    simulation.
    """
    await sleep(ms)
    return {"slept": ms}
