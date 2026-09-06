# Durable Execution in 5,000 Lines

Building a durable execution engine from first principles. Production-grade. Planet-scale.

## Intro

https://github.com/user-attachments/assets/176e33d3-54db-46ae-8d03-6f85529f8c25

## The Idea

A durable function survives the process that runs it. Crash the machine mid-flight,
redeploy, lose a region — the function picks up where it left off, without the
programmer writing a single line of retry, checkpoint, or recovery logic.

That is the whole promise. The interesting part is that it fits in about five
thousand lines.

## The Program

Here is the target — a research agent that plans searches, fans them out, and
synthesizes a cited report:

```python
@resonate
async def research(question: str):
    # Plan the searches
    queries = await agent(f"Plan the searches for: {question}")

    # Fan out the searches
    results = await gather(search.rpc(q) for q in queries)

    # Synthesize the results
    return await agent(f"Write a cited report. {question}: {results}")
```

Ordinary async/await. No state machines, no workflow DSL, no YAML, no context
object threaded through every call. One decorator.

`await agent(...)` runs durably in this process. `search.rpc(q)` runs durably in
another one — same function, different machine. `gather` does what it always did.

Kill this program at any point and restart it. It resumes.

## What We Build

- **A promise store** — the durable primitive everything else rests on: a promise
  that outlives the process holding it.
- **An execution model** — deterministic replay, memoized calls, structured
  concurrency across machines.
- **A scheduler** — leases, timeouts, retries, and the failure detection that
  makes them safe.
- **An RPC layer** — location-transparent calls between processes, with
  exactly-once-looking semantics on top of at-least-once delivery.
- **The distributed story** — how this scales from one laptop to many regions
  without changing the programming model.

## Layout

```
code/     the engine, built up in steps
notes/    the reasoning: designs, proofs, dead ends
design/   the site, and the posts it publishes
videos/   source video files
```

`notes/` is the part you cannot get from reading the source. Every non-obvious
decision in `code/` should have an entry explaining what the alternatives were and
why they lost.

Posts live in `design/content/writing` as MDX and are rendered by the site in
`design/`. See `design/README.md` for how to add one.

## Status

Early. The code is written to be read, not vendored — clarity beats cleverness,
and every abstraction has to earn its place against the line budget.
