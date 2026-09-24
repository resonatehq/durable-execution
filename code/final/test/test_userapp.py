"""What a user has to write, asserted as a contract rather than a README.

The claim is that a durable service is one file. Not one file plus a
routing table, plus a module list naming itself, plus an import of four
internal modules -- one file, whose only wiring is `handler = serve()`.

Every one of those qualifications was true at some point in this project's
life and was removed because of a test below. `ROUTES_APP` existed because
the deployed container had no functions in it; `ROUTES_WORKERS` had to name
every function even when they all ran in one service; the entry point came
from a module called `app`, which is one of the likeliest names to already
exist in somebody's project.

So this builds the user's repository in a temporary directory -- `main.py`
and nothing else -- and drives it through the loader Google's buildpack
uses. If the story ever needs a second file again, this is what says so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import resonate
from resonate.codec import decode, doc_key
from resonate.kernel import TAG_TARGET
from resonate.sdk import REGISTRY, TARGETS, dumps

ROOT = Path(__file__).parent.parent
BASE = "https://my-agent-abc.a.run.app"

#: The whole of a user's application. Names are deliberately unlike
#: anything else in the suite: `@resonate` registers globally, so a test
#: that borrowed a name would be testing whichever module imported last.
APP = '''
from resonate import gather, resonate, serve, sleep


@resonate
def lookup(word: str):
    return f"{word} means something"


@resonate
async def define(words: list):
    found = await gather(lookup.rpc(w) for w in words)
    return {"defined": found}


handler = serve()
'''


@pytest.fixture
def user_repo(tmp_path, monkeypatch):
    """A user's entire repository, and the environment Cloud Run gives it."""
    (tmp_path / "main.py").write_text(APP)
    # The second file, and the last one: Google's buildpack installs from
    # it. One line, because the package is one package.
    (tmp_path / "requirements.txt").write_text("resonate\n")

    monkeypatch.setenv("SIMULATED", "1")
    monkeypatch.setenv("BASE_URL", BASE)
    monkeypatch.delenv("ROUTES_WORKERS", raising=False)
    monkeypatch.delenv("ROUTES_ACCOUNT", raising=False)
    monkeypatch.syspath_prepend(str(ROOT))          # the package, as if installed

    before_registry, before_targets = dict(REGISTRY), dict(TARGETS)
    yield tmp_path
    sys.modules.pop("main", None)
    REGISTRY.clear(), REGISTRY.update(before_registry)
    TARGETS.clear(), TARGETS.update(before_targets)


def load(tmp_path):
    """The user's app, loaded and answering, and the server behind it."""
    import functions_framework

    client = functions_framework.create_app(
        "handler", str(tmp_path / "main.py")).test_client()
    assert client.get("/ready").status_code == 200
    return client, sys.modules["main"].handler.server


def test_the_user_writes_one_file(user_repo):
    """Two files in the repository, and only one of them is theirs."""
    assert sorted(p.name for p in user_repo.iterdir()) == ["main.py", "requirements.txt"]
    assert user_repo.joinpath("requirements.txt").read_text().strip() == "resonate"


def test_one_import_is_the_whole_of_the_wiring(user_repo):
    """No app object, no framework to start, no decorator on the entry
    point. `--function handler` wants a module-level function and
    `serve()` returns one."""
    load(user_repo)


def test_the_functions_register_with_nothing_naming_them(user_repo):
    """The entry point is their file, so importing it is the registration."""
    load(user_repo)
    assert {("lookup", 0), ("define", 0)} <= set(REGISTRY), sorted(REGISTRY)


def test_rpc_routes_itself_with_no_table(user_repo):
    """One service running everything is the shape a user starts in, and in
    that shape the routing table is derivable from `BASE_URL`."""
    assert "ROUTES_WORKERS" not in os.environ
    load(user_repo)
    assert TARGETS["lookup"] == f"{BASE}/execute"
    assert TARGETS["define"] == f"{BASE}/execute"


def test_a_split_deployment_still_overrides(user_repo, monkeypatch):
    """Naming one function says where it lives without saying anything
    about the rest, which is what makes the default safe to have."""
    monkeypatch.setenv("ROUTES_WORKERS", json.dumps(
        {"lookup": "https://lookup-svc.a.run.app/execute"}))
    load(user_repo)
    assert TARGETS["lookup"] == "https://lookup-svc.a.run.app/execute"
    assert TARGETS["define"] == f"{BASE}/execute", "the default was lost"


def test_a_whole_run_from_a_file_a_user_wrote(user_repo):
    """The claim, end to end: their code, their entry point, a durable run."""
    client, server = load(user_repo)
    queue, clock = server.engine.queue, server.clock
    started = client.post("/", json={"kind": "promise.create", "data": {
        "id": "define.1", "timeoutAt": clock() + 3_600_000,
        "param": {"data": dumps({"f": "define", "a": [["ping", "pong"]]}).data},
        "tags": {TAG_TARGET: f"{BASE}/execute"}}})
    assert started.status_code == 200, started.get_json()

    # Play the part Cloud Tasks plays: deliver what the queue is holding.
    for _ in range(200):
        delivery = queue.take(clock())
        if delivery is None:
            break
        path = delivery.url[len(BASE):] if delivery.url.startswith(BASE) else delivery.url
        answer = client.post(path, json=delivery.body or {})
        (queue.ack if answer.status_code < 400 else queue.nack)(delivery, clock())

    found = server.engine.store.get(doc_key("define.1"))
    assert found, "nothing was ever written"
    root = decode(found[0].encode(), "define.1").get("define.1").promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == {
        "defined": ["ping means something", "pong means something"]}


def test_without_serve_there_is_no_entry_point(user_repo):
    """Forgetting `handler = serve()` is a deploy that fails to load, and
    says which name it was looking for."""
    (user_repo / "main.py").write_text(APP.replace("handler = serve()", ""))
    with pytest.raises(Exception) as caught:
        load(user_repo)
    assert "handler" in str(caught.value), caught.value


def test_the_public_surface_is_small_and_deliberate():
    """Everything else is the engine. A user reaching past this list is a
    gap in it, not a clever workaround."""
    assert set(resonate.__all__) == {
        "serve", "resonate", "gather", "sleep", "external", "Failed", "Durable"}
    for name in resonate.__all__:
        assert hasattr(resonate, name), name


@pytest.mark.parametrize(
    "example", sorted(p for p in (ROOT / "examples").iterdir() if p.is_dir()),
    ids=lambda p: p.name)
def test_the_examples_are_user_applications_by_these_rules(example):
    """They are not privileged. Every example in this repository is written
    the way the contract above says a user writes one -- one import for the
    wiring, nothing reaching past the published surface -- so if one of them
    ever needs something a user could not have, the story has quietly
    stopped being true and this is what notices."""
    for source in sorted(example.glob("*.py")):
        src = source.read_text()
        for private in ("from resonate.sdk", "from resonate.server",
                        "from resonate.engine", "from resonate.kernel",
                        "from resonate.codec", "import resonate.sdk"):
            assert private not in src, f"{example.name}/{source.name} reaches into {private}"

    entry = (example / "main.py").read_text()
    assert "from resonate import" in entry, example.name
    assert "handler = serve()" in entry, example.name
