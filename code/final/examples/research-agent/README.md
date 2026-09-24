# The research agent

The program from the repository's README, as something you can deploy. It
is also the application `test_http.py` serves over a real socket, and
`test_deploy.py` checks it stays the README's program, so the thing being
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
| `main.py` | the four functions, and `handler = serve()` on the last line |
| `requirements.txt` | one line |

That is the whole application. `--target=handler` locally, and
`--function handler` on Cloud Run, look for a module-level function by that
name; `serve()` builds it from the environment.

Every function runs in this one service: with `BASE_URL` set, `serve()`
routes each of them to `BASE_URL/`, which is where `search.rpc(q)` sends its
dispatches. `ROUTES_WORKERS` (JSON, function name to URL) moves one
elsewhere.

## Running it

```bash
PYTHONPATH=../.. SIMULATED=1 functions-framework --target=handler --port 8080
```

`SIMULATED=1` swaps the bucket and the queue for in-memory ones and changes
nothing else: same engine, same kernel, same codec. `PYTHONPATH=../..` is
how the package is found while it is a sibling directory in this repository
rather than an install.

Start a run. Everything goes to `POST /`; the body's `kind` says what it is,
and the `resonate:target` tag says where the run's task is dispatched:

```bash
curl -sX POST localhost:8080/ -H 'content-type: application/json' -d '{
  "kind": "promise.create",
  "data": {"id": "research.1", "timeoutAt": 99999999999999,
           "param": {"data": "{\"f\": \"research\", \"a\": [\"What is durable execution?\"]}"},
           "tags": {"resonate:target": "http://localhost:8080/"}}}'
```

## Deploying it

```bash
gcloud run deploy research-agent --source . --function handler \
  --set-env-vars BUCKET=...,PROJECT=...,LOCATION=...,QUEUE=...,BASE_URL=...
```

`BASE_URL` is the service's own URL. Add `ROUTES_ACCOUNT` (and `AUDIENCE`)
to have Cloud Tasks sign its `execute` and `timeout` deliveries and the
service check them; `resonate/config.py` lists the rest.

One caveat, stated plainly because it has not been run. Only this directory
is uploaded, so the buildpack installs `resonate` from `requirements.txt` —
and the package is not published, so that line has to name where it really
is until it is:

```
resonate @ git+https://github.com/resonatehq/durable-execution@main#subdirectory=code/final
```

This exact requirements line is untested. `../travel-agent/` has the same
caveat.
