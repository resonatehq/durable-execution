# Durable Execution in 5,000 Lines

Building a durable execution engine from first principles. Production-grade. Planet-scale.

## The Idea

A durable function survives the process that runs it. Crash the machine mid-flight,
redeploy, lose a region — the function picks up where it left off, without the
programmer writing a single line of retry, checkpoint, or recovery logic.

That is the whole promise. The interesting part is that it fits in about five
thousand lines.

## The Program

Here is the target — a research agent that plans searches, fans them out, and
synthesizes a cited report:

```ts
async function research(context: Context, question: string) {
  // Plan the searches
  const queries = await context.run(agent,
    `Plan the searches for: ${question}`
  );

  // Fan out the searches
  const results = await Promise.allSettled(
    queries.map((q) => context.rpc(search, q))
  );

  // Synthesize the results
  return await context.run(agent,
    `Write a cited report. ${question}: ${results}`
  );
}
```

Ordinary async/await. No state machines, no workflow DSL, no YAML. `run` executes
a function durably in this process; `rpc` invokes a function durably in *another*
process. Both are resumable. `Promise.allSettled` does what it always did.

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
code/   the engine, built up in steps
notes/  the reasoning: designs, proofs, dead ends
```

`notes/` is the part you cannot get from reading the source. Every non-obvious
decision in `code/` should have an entry explaining what the alternatives were and
why they lost.

## Status

Early. The code is written to be read, not vendored — clarity beats cleverness,
and every abstraction has to earn its place against the line budget.
