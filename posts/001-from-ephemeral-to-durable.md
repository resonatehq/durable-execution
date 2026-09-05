# From Ephemeral to Durable

Here is an agent. It calls a model, runs whatever tools the model asks for,
feeds the results back, and repeats until the model stops asking.

```python
async def agent(prompt: str):
    messages = [{"role": "user", "content": prompt}]

    while True:
        response = await llm(messages)
        messages.append(response)

        if not response.tool_calls:
            return response.content

        for call in response.tool_calls:
            messages.append(await tool(call))
```

That is the whole program, and it is a good program. It is also ephemeral: it
exists only as long as the process running it. `messages`, the loop position,
the half-finished tool call — all of it lives in one process's memory. Kill the
process and it is gone.

For a function that runs in ten milliseconds, nobody cares. This one runs for
minutes. It makes twenty model calls and a dozen tool calls, and over those
minutes a deploy happens, a spot instance is reclaimed, an OOM killer picks a
victim. When that happens you start over from the first token: every model call
paid for twice, every tool call run twice. Nineteen successful steps are not
recoverable, because nothing wrote them down.

## Manual durability

So you write them down. Before touching the agent, decide what you are writing.

### The store

A step has an identity, and it either has a result or it does not. That is a
promise:

```python
@dataclass(frozen=True)
class Promise:
    id: str
    state: str          # "pending" | "resolved"
    value: Any = None
```

And a store holds them:

```python
class Store:
    def __init__(self):
        self._db: dict[str, Promise] = {}

    def create(self, id: str) -> Promise:
        return self._db.setdefault(id, Promise(id, "pending"))

    def resolve(self, id: str, value: Any) -> Promise:
        p = self._db[id]
        if p.state == "pending":
            self._db[id] = Promise(id, "resolved", value)
        return self._db[id]
```

This one is a dict, so it dies with the process and is useless for the actual
job. Ignore that for now — a real one is a database, and swapping it changes
nothing above this line. What matters is the interface, and specifically two
properties of it.

**`create` is idempotent.** Call it once or call it a thousand times with the
same id and you get the same promise. This is what makes replay work: a
restarted program calls `create` on ids that already exist and gets back what
happened last time.

**`resolve` is first-writer-wins.** It settles a pending promise and refuses to
settle a settled one, and it returns whatever is in the store afterward — not
necessarily what you passed in. That return value is the interesting part. If
two workers run the same step concurrently, both call `resolve`, and both walk
away with the *same* value: the first one's. The loser's result is discarded.
Disagreement is not possible, which is the property you need when you cannot
prevent a step from running twice.

Everything else in a durable execution engine is a policy on top of these two
operations. That is the claim of this post, and it is worth being suspicious of,
so watch how far it goes.

### Wrapping by hand

Now put the store in the agent. Every call site gets the same treatment: build
an id, create the promise, return early if it is settled, otherwise do the work
and settle it.

```python
async def agent(prompt: str, store: Store, run_id: str):
    messages = [{"role": "user", "content": prompt}]
    step = 0

    while True:
        step += 1
        id = f"{run_id}.{step}"

        p = store.create(id)
        if p.state == "resolved":
            response = p.value
        else:
            response = store.resolve(id, await llm(messages)).value

        messages.append(response)

        if not response.tool_calls:
            return response.content

        for call in response.tool_calls:
            step += 1
            id = f"{run_id}.{step}"

            p = store.create(id)
            if p.state == "resolved":
                result = p.value
            else:
                result = store.resolve(id, await tool(call)).value

            messages.append(result)
```

This works. It is what most teams end up with, and it is correct as written.
There is nothing wrong with it except the cost.

The obvious cost is that eight lines of agent became thirty, and the twenty are
all the same twenty. That part is easy to fix — factor it out:

```python
async def durable(store, id, func, *args):
    p = store.create(id)
    if p.state == "resolved":
        return p.value
    return store.resolve(id, await func(*args)).value
```

```python
response = await durable(store, f"{run_id}.{step}", llm, messages)
```

Good. The duplication is gone. Three things are not.

**The store and the run id are now part of the signature.** `agent` needs a
`store` and a `run_id` to be durable, so every caller needs them too, and so
does every function `agent` calls, all the way down. Durability leaks upward
through the type of every function that participates in it.

**The id is a counter, and the counter has to be right.** In a straight line of
code you can name your steps `"plan"`, `"search"`, `"report"`. In a loop there
is no fixed set of steps, so identity falls back on position — and position has
to land on the same number for the same call on every replay. That means the
replayed program has to take the same path through the loop. Recovery is only
correct if the function is deterministic between recorded steps, and nothing in
the code says so. Add an `if` on a timestamp, iterate a set, skip a tool call
when a flag is set, and the counter drifts. Step 7 on the replay is not step 7
from the first run, and the agent resumes with someone else's answer. No test
catches this; it shows up as an agent that occasionally continues a conversation
it never had.

**A step can still run twice.** Between `llm(messages)` returning and `resolve`
committing, the process can die. The call happened, the record did not, and on
restart you make it again. You can shrink that window but you cannot close it,
so the honest position is that steps execute at least once, and anything with an
external effect has to tolerate it. First-writer-wins is what keeps that from
turning into disagreement.

The third one is a property of the problem, not of this approach — it does not
change in the next section. The first two do.

## Automatic durability

Look at what `durable` actually does. It takes an identity, checks whether a
result is known, and if not, produces one and records it. Nothing in it mentions
agents, models, or tools. It is not specific to this program, and there is no
version of it that would be — the shape is the same for every call in every
durable function anyone will ever write.

The best solution is to stop threading that by hand and make it the primitive.
Persist promises, and let the call sites use them.

```python
@resonate
async def agent(prompt: str):
    messages = [{"role": "user", "content": prompt}]

    while True:
        response = await llm(messages)
        messages.append(response)

        if not response.tool_calls:
            return response.content

        for call in response.tool_calls:
            messages.append(await tool(call))
```

Same program as the top of this post. The bookkeeping did not move into a
helper; it moved under the language. `await llm(messages)` derives an id,
creates the promise, and returns its value if it is settled. If it is not, the
call runs and settles it. A crash and restart replays the function, finds the
promises that survived, and skips past the work they represent.

The store is gone from the signature — the decorator supplies it. The counter is
gone too. It still exists, but it is maintained by code whose only job is to
maintain it, so it is computed the same way on every replay rather than
depending on you not having written an `if` in the wrong place.

## Why promises

A promise is the right primitive because it is already the unit that both the
language and the network agree on.

The language agrees: `await` is how a Python program says *block until this
value is known*. Nothing about that requires the value to be produced in this
process, or within this process's lifetime. Point `await` at a persisted promise
and the same syntax means block until this value is known, wherever and whenever
it becomes known.

The network agrees: a remote call is a promise held by someone else. The caller
does not need to know which process settles it, or whether that process is the
one that started the work. It waits on an id.

From there the rest falls out. Recovery is reading ids you already have. Fan-out
is many ids. A timeout is a promise settled by a clock instead of a function. A
retry is a policy on an unsettled promise. Concurrency across machines is a
promise whose children are also promises.

None of that needs a workflow DSL, a state machine, or a YAML file describing
your control flow. The control flow is the Python. The durability is the store.

## What that leaves

The idea is small. The engineering is not, and that is where the rest of this
project goes:

- **A real store.** `create` and `resolve` have to keep those two properties
  under concurrent writers, across processes, across regions — which a dict does
  not.
- **The scheduler.** Something must decide which pending promises are being
  worked on, and by whom.
- **Leases and failure detection.** A worker that dies mid-call has to be
  noticed and its work reassigned, without two workers running the same step in
  parallel more often than necessary.
- **At-least-once, and living with it.** The window is still there. It gets
  handled once, in one place, instead of at every call site.

That is roughly five thousand lines. The next post replaces the dict.
