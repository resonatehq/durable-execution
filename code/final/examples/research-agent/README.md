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
SA=runtime@PROJECT.iam.gserviceaccount.com

gcloud run deploy research-agent --source . --function handler \
  --region REGION --service-account "$SA" --no-allow-unauthenticated \
  --set-env-vars BUCKET=...,PROJECT=...,LOCATION=...,QUEUE=...,BASE_URL=...,ROUTES_ACCOUNT="$SA"
```

`BASE_URL` is the service's own URL, and it has to be known before the
service exists — which sounds like a chicken and an egg and is not: a Cloud
Run URL is `https://<service>-<project number>.<region>.run.app`, so you
can write it down before the first deploy and it is right. `ROUTES_ACCOUNT`
(and `AUDIENCE`) have Cloud Tasks sign its `execute` and `timeout`
deliveries and the service check them; `resonate/config.py` lists the rest.

Only this directory is uploaded, so the buildpack installs `resonate` from
`requirements.txt` — and the package is not published, so that line has to
name where it really is until it is:

```
resonate @ git+https://github.com/resonatehq/durable-execution@<sha>#subdirectory=code/final
```

A branch works in place of a commit. A commit is better: the deploy then
says which engine is serving, and re-deploying does not silently move.

### What has run

2026-09-24, deployed exactly this way from exactly this directory. Three
runs, each one a claim the simulators can only make about themselves:

| | |
|---|---|
| a research run | resolved, six promises, nineteen commits. The plan, three `search.rpc` fan-outs delivered as three separate Cloud Tasks dispatches, the synthesis |
| `nap(60000)` | resolved `{"slept": 60000}`. The task went to `suspended` with one scheduled task and nothing running, and Cloud Tasks delivered the deadline a minute later |
| a destroyed dispatch | resolved anyway, six promises. The queue was paused, the run started, its only `execute` task deleted and the queue resumed: the deadline brought the run back. The document shows it — root task `version: 3`, against `version: 2` on an undisturbed run |

Two IAM grants, neither of which the code can ask for, and both of which a
deployment fails without:

- `roles/run.invoker` on the service for the account the queue signs with,
  or every delivery is a `403` that the service never sees. It is worth
  granting *before* the first run rather than after: IAM took about twenty
  seconds to propagate here, and the dispatches 403'd until it did. The
  queue retried through it, which is the design working, and it still makes
  a first run look slower than it is.
- `roles/iam.serviceAccountUser` on that account, held by itself. Running
  *as* an account is not permission to mint tokens *for* it; `queue_gcp.py`
  says more about why.

One thing to expect in the logs rather than be alarmed by: a `409` during
the fan-out's join, retried by the queue and then `200`. Two siblings
resolving at once contend for one object, and contention here is latency,
not loss.

`../travel-agent/` is the same shape and has not been deployed.
