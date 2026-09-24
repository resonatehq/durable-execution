"""Deciding what to do next, which is the one step that is not deterministic.

That is fine, and it is worth being precise about why. A durable function
may do anything it likes *inside* a call; what replay requires is that the
sequence of durable calls is the same every time. A model's answer is
recorded in a promise the moment it is produced, so the replay reads the
answer back rather than asking again -- the run is deterministic even
though the planner is not. It is the same property that lets a payment
succeed exactly once.

## Two planners

`plan` is a durable call either way. With credentials it asks Claude; with
none it follows `SCRIPT`, a planner that can complete this one goal and
nothing else. The script exists so the example runs, and the tests pass,
on a laptop with no key and no network -- and so that `test_example_agent.py` is
testing the agent rather than the weather inside a model.

## Why the output is a schema and not a parse

The tutorial this is translated from asks for JSON in the prompt and then
repairs what comes back: strip fences, find the first brace, try again.
`output_config.format` makes the API return JSON that validates against a
schema, so there is nothing to repair. What is left is the real question --
whether the *plan* is one we are willing to carry out -- and `main.py`
answers that rather than trusting the model.
"""

from __future__ import annotations

import json
import os
from typing import Any

from resonate import resonate

from tools import NEEDS_CONFIRMING, SIGNATURES

MODEL = "claude-opus-5"

#: What a plan may be. The model chooses a step; it does not get to choose
#: whether a step needs a person's approval -- `main.py` decides that from
#: `NEEDS_CONFIRMING`, because "should this be confirmed" is a property of
#: the tool and not an opinion.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "next": {"type": "string", "enum": ["tool", "ask", "done"]},
        "tool": {"type": ["string", "null"],
                 "enum": [*SIGNATURES, None]},
        "args": {"type": "object", "additionalProperties": True},
        "say": {"type": "string"},
    },
    "required": ["next", "tool", "args", "say"],
    "additionalProperties": False,
}

GOAL = (
    "You are a travel agent. Help the user, in this order: find an event to "
    "travel to, search flights around its dates, then invoice them for the "
    "flight they pick. Take one step at a time. Ask the user for anything "
    "you are missing rather than inventing it. When the invoice exists, say "
    'so and set next to "done".'
)


def _system() -> str:
    lines = [GOAL, "", "The tools you may name:"]
    for name, spec in SIGNATURES.items():
        args = ", ".join(f"{k} ({v})" for k, v in spec["args"].items())
        lines.append(f"- {name}: {spec['what']} Arguments: {args}")
    lines.append("")
    lines.append(
        "Set next to 'tool' with a tool and its args to act, 'ask' to put a "
        "question to the user, or 'done' when the goal is met. 'say' is what "
        "the user reads, always.")
    lines.append(
        f"These change something and will be confirmed with the user before "
        f"they run, so propose them normally: {', '.join(sorted(NEEDS_CONFIRMING))}.")
    return "\n".join(lines)


def available() -> bool:
    """Whether there is anything to authenticate with.

    Any of the three the SDK reads, not just the first: an unset
    `ANTHROPIC_API_KEY` does not mean there are no credentials, and a
    profile from `ant auth login` is the usual case on a laptop.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return bool(os.environ.get("ANTHROPIC_PROFILE")) or os.path.isdir(
        os.path.expanduser("~/.config/anthropic"))


@resonate
async def plan(history: list):
    """One step of the agent's thinking, as a durable call.

    Its answer is recorded before anything acts on it, so a crash between
    deciding and doing replays the decision rather than making a new one.
    A model asked twice would not agree with itself, and the run would
    resume into a plan its own history does not support.
    """
    if not available():
        return scripted(history)

    import anthropic  # here rather than at import, so no key means no import

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16_000,
        system=_system(),
        messages=[{"role": "user", "content": _transcript(history)}],
        output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        return {"next": "done", "tool": None, "args": {},
                "say": "I can't help with that."}
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _transcript(history: list) -> str:
    """The conversation as the model sees it. Rebuilt from the run's own
    durable calls every time, which is why it does not have to be stored
    anywhere: it is already in the document."""
    lines = []
    for who, what in history:
        lines.append(f"{who}: {what if isinstance(what, str) else json.dumps(what)}")
    lines.append("What is the next step?")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The planner for when there is no key
# ---------------------------------------------------------------------------


def scripted(history: list) -> dict[str, Any]:
    """Enough of a planner to finish this one goal.

    It reads the same history the model reads and picks the next unmet
    step. Not an agent -- it cannot do anything but book this trip -- but
    it exercises every path `main.py` has: a tool, a question, a
    confirmation, and an ending.
    """
    done = {what.get("tool") for who, what in history
            if who == "tool" and isinstance(what, dict)}
    said = " ".join(str(w) for _, w in history).lower()

    if "find_events" not in done:
        city = "New York City" if "new york" in said else _city(said)
        if city is None:
            return _ask("Which city would you like to travel to, and in which month?")
        return _do("find_events", {"city": city, "month": _month(said) or "May"},
                   f"Looking for events in {city}.")

    if "search_flights" not in done:
        event = _first_event(history)
        if event is None:
            return _ask("I found nothing there. Another city or month?")
        origin = "San Francisco" if "san francisco" in said else None
        if origin is None:
            return _ask(f"{event['name']} runs {event['from']} to {event['to']}. "
                        "Where are you flying from?")
        return _do("search_flights",
                   {"origin": origin, "destination": "New York City",
                    "depart": event["from"], "back": event["to"]},
                   f"Searching flights for {event['name']}.")

    if "create_invoice" not in done:
        flights = _last(history, "search_flights")["flights"]
        pick = min(flights, key=lambda f: f["price"])
        if "@" not in said:
            return _ask(f"Cheapest is {pick['number']} at ${pick['price']:.0f}. "
                        "What email should the invoice go to?")
        return _do("create_invoice",
                   {"flight": pick["number"], "amount": pick["price"],
                    "email": _email(said)},
                   f"Invoicing you for {pick['number']}.")

    invoice = _last(history, "create_invoice")
    return {"next": "done", "tool": None, "args": {},
            "say": f"Done. Your invoice is at {invoice['url']}."}


def _do(tool: str, args: dict, say: str) -> dict:
    return {"next": "tool", "tool": tool, "args": args, "say": say}


def _ask(say: str) -> dict:
    return {"next": "ask", "tool": None, "args": {}, "say": say}


def _last(history: list, tool: str) -> dict:
    return [what for who, what in history
            if who == "tool" and isinstance(what, dict) and what.get("tool") == tool][-1]


def _first_event(history: list):
    events = _last(history, "find_events")["events"]
    return events[0] if events else None


def _city(said: str):
    return "San Francisco" if "san francisco" in said else None


def _month(said: str):
    for m in ("january february march april may june july august september "
              "october november december").split():
        if m in said:
            return m.capitalize()
    return None


def _email(said: str) -> str:
    return next(w.strip(".,") for w in said.split() if "@" in w)
