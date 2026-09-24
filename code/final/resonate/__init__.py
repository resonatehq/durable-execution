"""Durable execution: ordinary async/await, one decorator, one import.

A durable function survives the process that runs it. Crash the machine
mid-flight, redeploy, lose a region -- it picks up where it left off,
without a line of retry, checkpoint or recovery logic in your code.

What you write is one file:

    from resonate import handler, resonate, gather   # noqa: F401

    @resonate
    def search(query: str):
        return index.query(query)

    @resonate
    async def research(question: str):
        queries = await agent(f"Plan the searches for: {question}")
        results = await gather(search.rpc(q) for q in queries)
        return await agent(f"Write a cited report. {question}: {results}")

Call it `main.py`, deploy it, and the run is durable. `handler` is the
whole of the wiring: Google's buildpack looks for a module-level function
of that name, and re-exporting ours puts one there. It is never called by
your code, which is why the `noqa` is not decoration -- a linter that
strips unused imports will delete your service's entry point.

## The five names

`resonate` marks a function durable. `gather` awaits several durable calls
at once, dispatching all of them before anything blocks. `sleep` waits,
durably, with nothing running while it does. `Failed` is what a durable
call raises when the call it is replaying was recorded as rejected --
catch it like any other exception. `handler` is the entry point above.

Everything else in this package is the engine, and you should not need to
import any of it. If you find yourself reaching for `kernel`, `engine` or
`sdk` directly, that is worth an issue rather than a workaround.

## What the deployment still has to say

Two things this file cannot know, because they are infrastructure you
created rather than code you wrote: `BUCKET`, the Cloud Storage bucket
that holds the documents, and `QUEUE`, the Cloud Tasks queue that carries
the work. `app.py` lists the rest and what each is derived from when it is
absent.
"""

from __future__ import annotations

from .app import handler
from .sdk import Durable, Failed, gather, resonate, sleep

__all__ = ["handler", "resonate", "gather", "sleep", "Failed", "Durable"]

#: Kept in step with `pyproject.toml`, which reads it rather than repeating
#: it -- two spellings of a version number is one spelling too many.
__version__ = "0.1.0"
