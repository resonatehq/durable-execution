"""A durable AI agent: one file, one loop, one durable call per step.

Translated from Temporal's "Build a Durable AI Agent" tutorial
(github.com/temporal-community/tutorial-temporal-ai-agent). Same agent --
plan a trip to an event, price the flights, invoice the traveller, with a
person confirming anything that changes something. What is different is
where the state lives, and that difference is most of the translation:

    Temporal                        here
    ------------------------------  ------------------------------------
    @workflow.defn class            @resonate async def
    self.conversation_history       a local list, rebuilt on every replay
    @workflow.signal user_prompt    await external(...)
    @workflow.signal confirm        await external(...)
    @workflow.signal end_chat       a message that says so
    workflow.wait_condition(...)    the await itself
    self.confirmed / waiting_for_   nothing; the promise is the state
    @workflow.query history         read the document
    @activity.defn                  @resonate
    execute_activity(name, args)    TOOLS[name](*args)
    continue_as_new                 a new run, started from outside

The three signals, the wait condition and the two boolean fields between
them are one thing here: `external`, a promise something outside settles.
There is no handler to register because there is nothing to hold -- a
suspended run has no memory, so the question has to be a promise in the
bucket, and once it is, the handler has nothing left to do.

The conversation history is a plain local list and is never stored. It
does not have to be: every value in it came from a durable call, so a
replay rebuilds it identically. That is worth pausing on, because it is
the part that looks like a bug and is not. `history` is thrown away every
time this function suspends and reconstructed from the document when it
resumes.

## What the model is not allowed to decide

Whether a step needs a person's approval. That is a property of the tool
(`NEEDS_CONFIRMING`), not an opinion, and a planner that could waive it
would be a planner that could be talked into waiving it. The model
proposes a step; this file decides whether a human sees it first, and
refuses any tool that is not in `TOOLS` or any argument the function does
not take.

## Running it

    SIMULATED=1 functions-framework --target=handler

Then start a run, read the document to find the pending question, and
answer it by settling that promise. `README.md` has the curl commands.
With no Anthropic credentials the planner falls back to a scripted one, so
this runs end to end on a laptop with no key.
"""

from __future__ import annotations

import inspect

from resonate import external, resonate, serve

from planner import plan
from tools import NEEDS_CONFIRMING, TOOLS

#: How long a question waits for an answer before it expires. An external
#: promise is not a timer, so an unanswered one is *rejected* -- the run
#: fails rather than silently carrying on as if the user had said yes.
PATIENCE = 60 * 60 * 1_000

#: When to stop and hand back a summary. Temporal calls this
#: continue-as-new and does it from inside the workflow. A run here cannot:
#: every operation is single-origin -- one document, one conditional write
#: -- and a new run is a new document, so only something outside can start
#: one. The run ends with `continue_from` instead, and whoever is driving
#: begins the next one with it. See README.md.
MAX_TURNS = 40

#: What a user says to stop.
END = "end"


@resonate
async def chat(opening: str | None = None):
    """One conversation, for as long as it takes.

    It may run for a month. Nothing is running while it waits -- no
    coroutine parked, no container held, no row marked in progress. What
    exists between two messages is a document in a bucket with one pending
    promise in it.
    """
    history: list[tuple[str, object]] = []
    if opening:
        # A summary handed over from a previous run, so the new one does not
        # start a stranger to the conversation it is continuing.
        history.append(("summary of earlier", opening))

    for _turn in range(MAX_TURNS):
        said = await external(ask={"for": "message"}, timeout=PATIENCE)
        if not isinstance(said, str) or said.strip().lower() == END:
            return {"ended": "the user said so", "conversation": history}
        history.append(("user", said))

        # Act until the agent needs the user again. A fan-out of reads could
        # be a `gather` here; it is a loop because these steps genuinely
        # depend on each other -- you cannot price a flight to an event you
        # have not found.
        while True:
            step = await plan(history)
            history.append(("agent", step["say"]))

            if step["next"] == "done":
                return {"ended": "the goal is met", "conversation": history,
                        "said": step["say"]}
            if step["next"] == "ask":
                break

            tool, args = step.get("tool"), step.get("args") or {}
            refusal = unacceptable(tool, args)
            if refusal:
                # Not an error and not a crash: the model proposed something
                # this agent will not do, which it is told about so it can
                # propose something else.
                history.append(("tool", {"tool": tool, "refused": refusal}))
                continue

            if tool in NEEDS_CONFIRMING:
                answer = await external(
                    ask={"confirm": tool, "args": args, "why": step["say"]},
                    timeout=PATIENCE)
                if str(answer).strip().lower() not in ("yes", "y", "ok", "true"):
                    history.append(("user", f"declined {tool}"))
                    break
                # Recorded, so the transcript says what the person agreed to
                # and not merely what happened afterwards. It is also what
                # the planner sees, which keeps it from asking twice.
                history.append(("user", f"confirmed {tool}"))

            history.append(("tool", {**await run(tool, args), "tool": tool}))

    return {"ended": "long enough", "conversation": history,
            "continue_from": await plan_summary(history)}


@resonate
async def run(tool: str, args: dict):
    """Call a tool, and treat its failure as an answer rather than a crash.

    A rejected durable call is a result: it is recorded, and replay reads
    the same rejection back rather than calling again. Handing it to the
    planner is what lets the agent apologise and try something else instead
    of the whole conversation dying on one bad argument.
    """
    fn = TOOLS[tool]
    ordered = [args[name] for name in inspect.signature(fn.fn).parameters]
    try:
        return await fn(*ordered)
    except Exception as e:                      # the tool's own answer
        return {"failed": f"{type(e).__name__}: {e}"}


@resonate
async def plan_summary(history: list):
    """Two sentences, so the next run is not a stranger to this one."""
    step = await plan([*history, ("user", "Summarise this conversation in two "
                                          "sentences so it can be continued.")])
    return step["say"]


def unacceptable(tool, args: dict) -> str | None:
    """Whether this agent is willing to carry the model's proposal out.

    A planner is a language model's opinion about what to do next. It
    arrives as data, over the same wire as everything else, and it is
    checked here against what these functions actually are -- not because
    the model is expected to lie, but because a step that runs only when
    it type-checks is one fewer thing that can go wrong at three in the
    morning.
    """
    if tool not in TOOLS:
        return f"no such tool; this agent has {', '.join(sorted(TOOLS))}"
    if not isinstance(args, dict):
        return "arguments must be an object"
    takes = set(inspect.signature(TOOLS[tool].fn).parameters)
    missing, extra = takes - set(args), set(args) - takes
    if missing:
        return f"missing {', '.join(sorted(missing))}"
    if extra:
        return f"{tool} does not take {', '.join(sorted(extra))}"
    return None


handler = serve()
