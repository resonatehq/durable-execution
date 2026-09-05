# From Ephemeral to Durable

Here is a research agent. It plans a set of searches, runs them, and writes a
cited report.

```python
async def research(question: str):
    queries = await agent(f"Plan the searches for: {question}")
    results = await gather(search(q) for q in queries)
    return await agent(f"Write a cited report. {question}: {results}")
```

The program is correct. It is also ephemeral: it exists only as long as the
process running it. The call stack, the local variables, the half-finished
`gather` — all of it lives in one process's memory. Kill the process and it is
gone.

For a function that runs in ten milliseconds, nobody cares. This one runs for
four minutes, calls a model twice, and fans out to a dozen searches. Over four
minutes a deploy happens, a spot instance is reclaimed, an OOM killer picks a
victim. When that happens you restart from the top: plan the searches again, pay
for the tokens again, run the searches again. The work that already succeeded is
not recoverable, because nothing wrote it down.

## Manual durability

So you write it down. Give each execution an id, record each step's result as it
completes, and check for a recorded result before doing the work.

```python
async def research(question: str, run_id: str):
    row = await db.fetch_one(
        "select result from steps where run_id = $1 and step = 'plan'",
        run_id,
    )
    if row is None:
        queries = await agent(f"Plan the searches for: {question}")
        await db.execute(
            "insert into steps (run_id, step, result) values ($1, 'plan', $2)",
            run_id, dumps(queries),
        )
    else:
        queries = loads(row["result"])

    # ... and the same again for every search, and again for the report
```

This works. It is what most teams end up with, and there is nothing wrong with
it except the cost.

The cost is that three lines of business logic became thirty lines of
bookkeeping, and the bookkeeping is the part that is easy to get wrong. Every
step needs an identity that is stable across restarts — `'plan'` is fine until
the function is called twice in one run. The fan-out needs a row per search and
a way to resume when four of twelve finished. Every new step is a new
opportunity to forget the check, or to write the record under the wrong key, or
to record a result the caller never actually returned.

And one problem does not go away no matter how carefully you write it. Between
the moment the model call returns and the moment the row commits, the process
can die. The work happened; the record did not. On restart you do it again. You
can shrink that window but you cannot close it, so the honest position is that
steps are executed at least once, and anything with an external effect has to
tolerate that.

That constraint is not a flaw in this approach. It is a property of the problem.
Note it, because it does not change when the approach does.

## Automatic durability

Look at what the manual version writes down. For every step, one record:

- an identity for the call,
- whether its result is known,
- and if it is known, the value.

That is the same shape every time. It does not depend on what the step does. It
is not specific to `research`, to agents, or to search. It is a promise —
pending, or settled with a value — with a name and a home outside the process.

The best solution is to stop writing that record by hand and make it the
primitive. Persist promises, and let the function call sites use them.

```python
@resonate
async def research(question: str):
    queries = await agent(f"Plan the searches for: {question}")
    results = await gather(search.rpc(q) for q in queries)
    return await agent(f"Write a cited report. {question}: {results}")
```

Same program as the top of this post. The bookkeeping did not move into a
helper; it moved under the language. `await agent(...)` names a promise, looks
for it, and returns its value if it is already settled. If it is not, the call
runs and settles it. A crash and restart replays the function, finds the
promises that survived, and skips straight past the work they represent.

## Why promises

A promise is the right primitive because it is already the unit that both the
language and the network agree on.

The language agrees: `await` is how a Python program says "block until this
value is known". Nothing about that requires the value to be produced in this
process, or within this process's lifetime. Point `await` at a persisted promise
and the same syntax means "block until this value is known, wherever and
whenever it becomes known".

The network agrees: an RPC is a promise held by someone else. `search.rpc(q)`
creates a promise here and lets another process settle it. The caller does not
need to know which process, or whether that process is the one that started the
work. It waits on an id.

From there the rest falls out. Fan-out is many ids. Recovery is reading ids you
already have. A timeout is a promise settled by a clock instead of a function. A
retry is a policy on an unsettled promise. Structured concurrency across
machines is a promise whose children are also promises.

None of that needs a workflow DSL, a state machine, or a YAML file describing
your control flow. The control flow is the Python. The durability is the store.

## What that leaves

The idea is small. The engineering is not, and it is where the rest of this
project goes:

- **The store.** Promises have to be created and settled exactly once, under
  concurrent writers, across regions.
- **The scheduler.** Something must decide which pending promises are being
  worked on, and by whom.
- **Leases and failure detection.** A worker that dies mid-call must be noticed,
  and its work reassigned — without two workers running the same step in
  parallel more often than necessary.
- **At-least-once, and living with it.** The window from the manual version is
  still there. It gets handled once, in one place, instead of at every call
  site.

That is roughly five thousand lines. The next post starts on the store.
