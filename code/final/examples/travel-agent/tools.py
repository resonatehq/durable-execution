"""The three things this agent can actually do.

A tool is an ordinary durable function. There is no tool class, no
registry of handlers, no dynamic dispatch by name in the worker: the
planner names one, `main.py` looks it up in `TOOLS`, and calling it is a
durable call like any other -- memoised by position, recorded in the
document, and read back rather than repeated if the run replays.

They are fixtures rather than live integrations on purpose. What this
example is about is the shape of the conversation and where the durable
boundaries fall, and a live flight API would make the interesting part
untestable while teaching nothing about it.
"""

from __future__ import annotations

from resonate import resonate

#: Two cities, three events. Enough to plan a trip around and small enough
#: that a person can check the agent's answer by reading this.
EVENTS = {
    "new york city": [
        {"name": "Vivid New York City", "from": "2026-05-01", "to": "2026-05-14"},
        {"name": "NYC Marathon", "from": "2026-11-01", "to": "2026-11-01"},
    ],
    "san francisco": [
        {"name": "Bay Area Science Festival", "from": "2026-05-08", "to": "2026-05-20"},
    ],
}

#: Deterministic, and deliberately not the cheapest-first order, so that an
#: agent claiming to have found the cheapest flight has had to look.
FLIGHTS = [
    {"number": "UA512", "price": 980.0},
    {"number": "AA101", "price": 850.0},
    {"number": "DL77", "price": 1120.0},
]


@resonate
def find_events(city: str, month: str):
    """Events in a city, in a month. Month is a name: 'May'."""
    found = [
        e for e in EVENTS.get(city.strip().lower(), [])
        if month.strip().lower()[:3] == _month_name(e["from"])[:3]
    ]
    return {"events": found} if found else {
        "events": [], "note": f"nothing in {city} in {month}; try New York City in May"}


@resonate
def search_flights(origin: str, destination: str, depart: str, back: str):
    """Return flights for a date range. Prices do not depend on the dates,
    which is a fixture's privilege and not a claim about airlines."""
    return {
        "flights": [{**f, "origin": origin, "destination": destination,
                     "depart": depart, "back": back} for f in FLIGHTS],
        "cheapest": min(FLIGHTS, key=lambda f: f["price"])["number"],
    }


@resonate
def create_invoice(flight: str, amount: float, email: str):
    """The step worth confirming before it runs, which is why this example
    has a confirmation step at all: the other two only read."""
    return {
        "invoice": f"inv_{flight.lower()}",
        "amount": amount,
        "sent_to": email,
        "url": f"https://invoices.example.com/inv_{flight.lower()}",
    }


#: What the planner is allowed to name. `main.py` will not call anything
#: that is not in here, whatever the model says -- a planner is a language
#: model's opinion about what to do next, not an instruction.
TOOLS = {
    "find_events": find_events,
    "search_flights": search_flights,
    "create_invoice": create_invoice,
}

#: Which of them change something. Only these need a person to say yes.
NEEDS_CONFIRMING = {"create_invoice"}


def _month_name(iso: str) -> str:
    return ["january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november",
            "december"][int(iso[5:7]) - 1]


#: What the model is told each tool takes. Kept beside the functions so the
#: description and the signature move together; `planner.py` turns it into
#: a JSON schema.
SIGNATURES = {
    "find_events": {
        "what": "Find events in a North American city in a given month.",
        "args": {"city": "the city to search", "month": "a month name, e.g. May"},
    },
    "search_flights": {
        "what": "Search return flights between two cities for a date range.",
        "args": {"origin": "departure city", "destination": "arrival city",
                 "depart": "ISO date", "back": "ISO date"},
    },
    "create_invoice": {
        "what": "Invoice the traveller for a flight. Changes something, so it "
                "is confirmed with the user before it runs.",
        "args": {"flight": "flight number", "amount": "price in dollars",
                 "email": "where to send it"},
    },
}
