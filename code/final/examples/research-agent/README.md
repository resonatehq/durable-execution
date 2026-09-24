# The research agent

The program from the repository's README, as something you can deploy. It
is also the example every test in this project drives, so the thing being
demonstrated and the thing under test are one file rather than two that
drift apart.

```python
@resonate
async def research(question: str):
    queries = await agent(f"Plan the searches for: {question}")
    results = await gather(search.rpc(q) for q in queries)
    return await agent(f"Write a cited report. {question}: {results}")
```

Ordinary async/await. `await agent(...)` runs durably in this process;
`search.rpc(q)` runs durably on another machine, same function; `gather`
does what it always did. Nothing in it mentions promises, tasks, leases,
retries or recovery, which is the claim the whole project is making.

`nap` is here too — the smallest durable sleep worth deploying. Nothing
runs while it waits: the worker suspends, the container is free to go away,
and a deadline in the document brings the run back.

## What is in it

| file | |
|---|---|
| `main.py` | the four functions, and `handler` re-exported in one import |
| `requirements.txt` | one line |

That is the whole application. `handler` is never called by this code —
`--function handler` looks for a module-level name and the import is what
puts one there, which is why the `noqa` on it is load-bearing rather than
decoration.

## Running it

```bash
PYTHONPATH=../.. SIMULATED=1 functions-framework --target=handler --port 8080
```

`SIMULATED=1` swaps the bucket and the queue for in-memory ones and changes
nothing else: same engine, same kernel, same codec. `PYTHONPATH=../..` is
how the package is found while it is a sibling directory in this repository
rather than an install.

Start a run:

```bash
curl -sX POST localhost:8080/ -H 'content-type: application/json' -d '{
  "kind": "promise.create",
  "data": {"id": "research.1", "timeoutAt": 99999999999999,
           "param": {"data": "{\"f\": \"research\", \"a\": [\"What is durable execution?\"]}"},
           "tags": {"resonate:target": "http://localhost:8080/execute"}}}'
```

## Deploying it

```bash
gcloud run deploy research-agent --source . --function handler \
  --set-env-vars BUCKET=...,PROJECT=...,LOCATION=...,QUEUE=...,BASE_URL=...
```

One caveat, stated plainly because it is the one thing here that has not
been run. Only this directory is uploaded, so the buildpack installs
`resonate` from `requirements.txt` — and the package is not published, so
that line has to name where it really is until it is:

```
resonate @ git+https://github.com/resonatehq/durable-execution@main#subdirectory=code/final
```

An earlier layout kept `main.py` at the root of `code/final` beside the
package, and *that* has run on Cloud Run — a full research run, six
promises, twenty-two commits. Moving the examples into directories of their
own made each of them a user application by the same rules as any other,
which is worth more than the shortcut, but it does mean this exact
requirements line is untested. `../travel-agent/` has the same caveat.
