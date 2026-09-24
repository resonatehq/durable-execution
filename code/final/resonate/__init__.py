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

## The six names

`resonate` marks a function durable. `gather` awaits several durable calls
at once, dispatching all of them before anything blocks. `sleep` waits,
durably, with nothing running while it does. `external` waits for somebody
outside to answer -- a person approving, a webhook, a form -- which is what
other systems spell as a signal handler and a wait condition. `Failed` is what a durable
call raises when the call it is replaying was recorded as rejected --
catch it like any other exception. `handler` is the entry point above.

## Two names, and versions

A durable function's name is not a label. A promise records the call it
stands for as `{"f": "process"}` and a worker looks the code up by it, so
two functions answering to one name means a dispatch created for one runs
the other. `@resonate` refuses that rather than letting the last import
win -- a `billing.py` and an `orders.py` both defining `process` is a
`DuplicateFunction` at start-up instead of a wrong answer in production.

If they really are two generations of one function, say so:

    @resonate(version=1)
    async def process(order): ...

and both stay deployed. That is what versions are for. A run replays from
the top and reads its earlier calls back *by position*, so inserting a
durable call or reordering two moves every position after it, and a run
already in flight would resume into a body that disagrees with its own
history. Under a version, it finishes on the body it started on, and new
runs take the new one. Unversioned means version 0, writes exactly the
bytes it wrote before versions existed, and is what you want until the day
you change a durable function with runs in the air.

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

from typing import TYPE_CHECKING

from .sdk import Durable, Failed, external, gather, resonate, sleep

if TYPE_CHECKING:  # for a type checker and an editor; never at run time
    from .app import handler


def __getattr__(name: str):
    """`handler`, imported only if somebody asks for it.

    It lives in `app.py`, which pulls in Flask, Werkzeug and the function
    framework -- 83ms and 228 modules, measured. An entry point needs all
    of that and pays for it gladly. A module of durable functions that is
    *not* the entry point does not, and neither does a test, and until this
    existed they paid anyway because the package imported `app` eagerly.

    `from resonate import handler` still works and still costs what it
    costs; `from resonate import resonate, gather` now costs 53ms instead
    of 136. The same discipline `store_gcp.py` follows for `google`.
    """
    if name == "handler":
        from .app import handler

        return handler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["handler", "resonate", "gather", "sleep", "external",
           "Failed", "Durable"]

#: Kept in step with `pyproject.toml`, which reads it rather than repeating
#: it -- two spellings of a version number is one spelling too many.
__version__ = "0.1.0"
