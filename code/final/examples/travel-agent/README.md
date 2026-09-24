# A durable AI agent

A translation of Temporal's [Build a Durable AI Agent][tutorial] tutorial
([source][repo]). Same agent: plan a trip to an event, price the flights,
invoice the traveller, with a person confirming the step that spends money.

The interesting part is not that it works. It is which pieces disappeared.

[tutorial]: https://learn.temporal.io/tutorials/ai/durable-ai-agent/
[repo]: https://github.com/temporal-community/tutorial-temporal-ai-agent

## The translation

| Temporal | here |
|---|---|
| `@workflow.defn class AgentGoalWorkflow` | `@resonate async def chat` |
| `self.conversation_history` | a local list, rebuilt on every replay |
| `@workflow.signal user_prompt` | `await external(ask={"for": "message"})` |
| `@workflow.signal confirm` | `await external(ask={"confirm": ...})` |
| `@workflow.signal end_chat` | a message that says `end` |
| `workflow.wait_condition(lambda: ...)` | the `await` itself |
| `self.confirmed`, `self.waiting_for_confirm` | — |
| `self.prompt_queue` | — |
| `@workflow.query get_conversation_history` | read the document |
| `@activity.defn` | `@resonate` |
| `execute_activity(name, args)` + dynamic dispatch | `TOOLS[name](*args)` |
| `RetryPolicy(...)` per call | the task's own deadline |
| `continue_as_new` | a new run, started from outside |

Three signals, a wait condition, a queue and two boolean fields become one
thing: `external`, a promise that something outside settles. There is no
handler to register because there is nothing to hold. A suspended run here
has no memory at all, so the question has to *be* a promise in the bucket —
and once it is, the handler has nothing left to do.

The queue goes for the same reason. `prompt_queue` exists in the Temporal
version because a signal can arrive while the workflow is busy and has to
be parked somewhere. Here a message is an answer to a specific promise, at
a specific position, so there is no "somewhere" for it to be parked in and
nothing to drain.

## The history is not stored anywhere

`chat` keeps the conversation in an ordinary local list. Nothing writes it
down. It is thrown away every time the function suspends — which is most of
the time, since the agent spends its life waiting for someone to type —
and rebuilt from scratch when the run resumes.

It comes back identical because every value in it came from a durable call:
the user's messages are the values of the promises they settled, and the
planner's answers are the values of its own. Replay reads them back in
order and the list reassembles itself. `test_example_agent.py` asserts this
directly, mid-conversation, by reading the user's words out of the promises
rather than out of anything the run wrote.

This is also why there is no query handler. The conversation is in the
bucket, in a document anyone can `GET`, whether or not anything is running.
Temporal's `get_conversation_history` query needs a live workflow to answer
it; here the last container exited three weeks ago and the answer is still
there.

## What the model is not allowed to decide

Whether a step needs a person's approval. In the tutorial the LLM returns
`next: "confirm"` and the workflow believes it. Here the model proposes a
step and `NEEDS_CONFIRMING` — a set in `tools.py` — decides whether a human
sees it first. A planner that could waive a confirmation is a planner that
can be talked into waiving one.

`unacceptable()` does the rest: a tool that is not in `TOOLS` is refused,
and so is any argument the function does not take. The refusal goes back to
the planner as a result, so the agent says something sensible instead of
crashing. A plan is data that arrived over a wire, and it is checked like
any other.

The tutorial also has to repair the model's JSON — strip code fences, find
the first brace, try again. `planner.py` asks for a schema instead
(`output_config.format`), so there is nothing to repair.

## The one thing that did not translate

`continue_as_new`. Temporal's workflow summarises itself after 250 messages
and restarts, from inside, carrying the summary.

A run here cannot do that, and the reason is structural rather than a
missing feature. Every operation in this system is **single-origin**: it
touches one document and commits with one conditional write. A new run is a
new document, so starting one is a second origin, and no operation may span
two. `chat` returns `{"ended": "long enough", "continue_from": <summary>}`
instead, and whoever is driving the conversation starts the next run with
it — one line, outside.

That is a real difference and not a wash. Temporal's version is more
convenient. This one has a property Temporal's does not: there is exactly
one writer per document, which is what makes a single conditional write
sufficient and the whole engine about five thousand lines.

## Running it

No Anthropic credentials needed — `planner.py` falls back to a scripted
planner that can complete this one goal, which is also what makes the test
a test of the agent rather than of the weather inside a model. With a key
(or an `ant auth login` profile) it asks Claude instead.

```bash
cd examples/travel-agent
PYTHONPATH=../.. SIMULATED=1 functions-framework --target=handler --port 8080
```

Start a conversation:

```bash
curl -sX POST localhost:8080/ -H 'content-type: application/json' -d '{
  "kind": "promise.create",
  "data": {"id": "trip.1", "timeoutAt": 99999999999999,
           "param": {"data": "{\"f\": \"chat\", \"a\": []}"},
           "tags": {"resonate:target": "http://localhost:8080/"}}}'
```

Find what it is waiting for — the pending promise tagged
`resonate:external`, whose parameter says what is being asked:

```bash
curl -s localhost:8080/ -X POST -H 'content-type: application/json' \
  -d '{"kind": "promise.get", "data": {"id": "trip.1:1"}}'
```

Answer it:

```bash
curl -sX POST localhost:8080/ -H 'content-type: application/json' -d '{
  "kind": "promise.settle",
  "data": {"id": "trip.1:1", "state": "resolved",
           "value": {"data": "\"an event in new york city in may\""}}}'
```

Then repeat. Each answer wakes the run, which asks its next question at the
next free position — `trip.1:1`, `:5`, `:9`, `:11` for the four in a full
booking, the gaps being the planner and the tools in between. Do not
compute them; read the document and take the pending one, which is what a
front end would do. A front end would read
the document, render the pending question, and POST the answer; that is the
whole of the integration, and it is why there is no API layer here.

## Files

| file | |
|---|---|
| `main.py` | the agent: the loop, the confirmation gate, the refusal check |
| `tools.py` | three durable functions and which of them change something |
| `planner.py` | one step of thinking — Claude with a schema, or a scripted planner |
| `requirements.txt` | `resonate`, and `anthropic` for when there is a key |
