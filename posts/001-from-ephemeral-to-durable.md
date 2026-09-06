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

## Where this sits

Running a durable function splits in two, and it is worth naming both halves
before starting, because only one of them is the subject here.

`execute_until_blocked_outer` deals with the world outside the function. It
takes a task off a queue, claims it so that no one else picks up the same work,
runs the function, and then completes or reschedules the task depending on how
that went:

```python
async def execute_until_blocked_outer(queue, store):
    task = await queue.claim()
    if task is None:
        return

    await execute_until_blocked_inner(store, task)
    await queue.complete(task)
```

That is close to all of it. The outer half is short, and the parts of it that
are hard — what a claim means when the claimant dies, how long a claim lasts —
are properties of the queue, not of this function.

`execute_until_blocked_inner` is the part that runs your code: start the
function, or restart it, and carry it forward until it finishes or reaches
something it cannot make progress on by itself. Everything from here on is
inside that call.

The name promises blocking, and nothing in this post blocks. The agent's calls
all complete in the same process, so the inner half always runs the function to
the end. Blocking shows up when a call is settled by someone else — a remote
call, a timer, a human — and that is a later post. The machinery below is what
makes it possible.

## Manual durability

So you write them down. Before touching the agent, decide what you are writing.

### The store

A step has an identity, and it has either succeeded, failed, or neither yet.
That is a promise:

```python
@dataclass(frozen=True)
class Promise:
    id: str
    state: str          # "pending" | "resolved" | "rejected"
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
        return self._settle(id, "resolved", value)

    def reject(self, id: str, error: Exception) -> Promise:
        return self._settle(id, "rejected", error)

    def _settle(self, id: str, state: str, value: Any) -> Promise:
        p = self._db[id]
        if p.state == "pending":
            self._db[id] = Promise(id, state, value)
        return self._db[id]
```

This one is a dict, so it dies with the process and is useless for the actual
job. Ignore that for now — a real one is a database, and swapping it changes
nothing above this line. What matters is the interface, and three properties of
it.

**`create` is idempotent.** Call it once or a thousand times with the same id
and you get the same promise. This is what makes replay work: a restarted
program calls `create` on ids that already exist and gets back what happened
last time.

**Settling is first-writer-wins.** `resolve` and `reject` settle a pending
promise and refuse to re-settle a settled one. A step that runs twice cannot
produce two different outcomes of record.

**Settling returns the promise, and it is not necessarily yours.** This is the
one that is easy to skip. `_settle` returns whatever is in the store *after* the
write — which, if someone beat you to it, is their outcome and not the one you
just passed in. So the rule at every call site is: settle, then read what came
back, then return or raise from that. Never from your local result.

Skipping it looks harmless until two workers run the same step. Worker A calls
the model and gets a response. Worker B calls the model and its request times
out. Both settle the promise; the store keeps whichever landed first, say A's
value. If B raises its own timeout instead of reading back, the two workers now
disagree about what that step did, and they carry that disagreement forward into
two different message histories for the same run. The promise is the record. If
you do not read it, you are not using it.

The same rule covers restarts, not just races. On replay the promise may already
be rejected from the first run. You raise the stored error. You do not call the
model again to find out.

Everything else in a durable execution engine is a policy on top of these three
properties. That is the claim of this post, and it is worth being suspicious of,
so watch how far it goes.

### Wrapping by hand

Now put the store in the agent. Every call site gets the same treatment: build
an id, create the promise, do the work if it is still pending, settle it either
way, then return or raise based on what the store says.

```python
async def agent(prompt: str, store: Store, run_id: str):
    messages = [{"role": "user", "content": prompt}]
    step = 0

    while True:
        step += 1
        id = f"{run_id}.{step}"

        p = store.create(id)
        if p.state == "pending":
            try:
                p = store.resolve(id, await llm(messages))
            except Exception as e:
                p = store.reject(id, e)

        if p.state == "rejected":
            raise p.value
        response = p.value

        messages.append(response)

        if not response.tool_calls:
            return response.content

        for call in response.tool_calls:
            step += 1
            id = f"{run_id}.{step}"

            p = store.create(id)
            if p.state == "pending":
                try:
                    p = store.resolve(id, await tool(call))
                except Exception as e:
                    p = store.reject(id, e)

            if p.state == "rejected":
                raise p.value

            messages.append(p.value)
```

This works. It is what most teams end up with, and it is correct as written.
There is nothing wrong with it except the cost.

Note the `p =` on both settle branches. That assignment is the whole third
property. Write `store.resolve(id, value)` without it and the code still runs,
still passes tests, and quietly returns local outcomes that the store disagrees
with. It fails only when two workers overlap, which is exactly the case the
store exists for.

The obvious cost is that eight lines of agent became thirty-five, and the
twenty-seven are all the same twenty-seven. That part is easy to fix — factor it
out:

```python
async def durable(store, id, func, *args):
    p = store.create(id)
    if p.state == "pending":
        try:
            p = store.resolve(id, await func(*args))
        except Exception as e:
            p = store.reject(id, e)

    if p.state == "rejected":
        raise p.value
    return p.value
```

```python
response = await durable(store, f"{run_id}.{step}", llm, messages)
```

Good. The duplication is gone, and the read-back is now written once instead of
at every call site. Three things are not gone.

**The store and the run id are part of the signature.** `agent` needs a `store`
and a `run_id` to be durable, so every caller needs them, and so does every
function `agent` calls, all the way down. Durability leaks upward through the
type of every function that participates in it.

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

**A step can still run twice.** Between `llm(messages)` returning and the settle
committing, the process can die. The call happened, the record did not, and on
restart you make it again. You can shrink that window but you cannot close it,
so the honest position is that steps execute at least once, and anything with an
external effect has to tolerate it. First-writer-wins plus reading back is what
keeps that from turning into disagreement.

The third one is a property of the problem, not of this approach — it does not
change in the next section. The first two do.

## Automatic durability

Look at what `durable` actually does. It takes an identity, checks whether an
outcome is known, produces one if it is not, records it, and reports whatever
the store ended up holding. Nothing in it mentions agents, models, or tools. It
is not specific to this program, and there is no version of it that would be —
the shape is the same for every call in every durable function anyone will ever
write.

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
creates the promise, runs the call if it is pending, settles it, reads it back,
and returns or raises from the stored outcome. A crash and restart replays the
function, finds the promises that survived, and skips past the work they
represent.

Here is what that does to the stack for one call. `agent` is running; it reaches
`await llm(messages)`:

```
        1                   2                   3                   4

                    ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
                    │ create promise │  │ llm(messages)  │  │ settle promise │
┌────────────────┐  ├────────────────┤  ├────────────────┤  ├────────────────┤
│ invoke         │  │ invoke         │  │ invoke         │  │ invoke         │
├────────────────┤  ├────────────────┤  ├────────────────┤  ├────────────────┤
│ agent          │  │ agent          │  │ agent          │  │ agent          │
└────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘
```

The call site no longer calls `llm`. It calls `invoke`, and `invoke` is the
runtime's frame — it derives the id, creates the promise, calls the real
function, and settles the promise with whatever came back. The frames above
`invoke` are always these three, in this order, whatever the call underneath
happens to be. That is the same shape the `durable` helper had; it has only
stopped being something you type.

Step 1 is the interesting one on a restart. `invoke` derives the same id, finds
the promise already settled from a previous life of this program, and returns
its value without ever reaching step 3. The model is not called again.

Every frame in that picture disappears when the process dies. The promise
created in step 2 does not. That is the entire trick: the stack is ephemeral, so
put the record somewhere the stack is not.

The store is gone from the signature — the decorator supplies it. The counter is
gone too. It still exists, but it is maintained by code whose only job is to
maintain it, so it is computed the same way on every replay rather than
depending on you not having written an `if` in the wrong place. And the `p =`
you have to remember thirty times is written once, in the runtime, where
forgetting it is a bug someone else already found.

## Why promises

A promise is the right primitive because it is already the unit that both the
language and the network agree on.

The language agrees: `await` is how a Python program says *block until this
value is known*. Nothing about that requires the value to be produced in this
process, or within this process's lifetime. Point `await` at a persisted promise
and the same syntax means block until this value is known, wherever and whenever
it becomes known. `raise` comes along for free — a rejected promise is an
exception that survived a restart.

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

- **A real store.** `create` and `_settle` have to keep those three properties
  under concurrent writers, across processes, across regions — which a dict does
  not. In particular, settle-and-return-the-winner has to be one atomic
  operation, not a read followed by a write.
- **The scheduler and the queue.** Something must decide which pending promises
  are being worked on and by whom, and hand them to the outer half as tasks.
- **Leases and failure detection.** A worker that dies mid-call has to be
  noticed and its work reassigned, without two workers running the same step in
  parallel more often than necessary.
- **At-least-once, and living with it.** The window is still there. It gets
  handled once, in one place, instead of at every call site.

That is roughly five thousand lines. The next post replaces the dict.
