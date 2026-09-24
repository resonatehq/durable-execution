"""What a user has to write, asserted as a contract rather than a README.

The claim is that a durable service is one file. Not one file plus a
routing table, plus a module list naming itself, plus an import of four
internal modules -- one file, whose only wiring is a single import.

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
from resonate import app, local
from resonate.codec import decode, doc_key
from resonate.kernel import TAG_TARGET
from resonate.sdk import REGISTRY, TARGETS, dumps

ROOT = Path(__file__).parent.parent
BASE = "https://my-agent-abc.a.run.app"

#: The whole of a user's application. Names are deliberately unlike
#: anything else in the suite: `@resonate` registers globally, so a test
#: that borrowed a name would be testing whichever module imported last.
APP = '''
from resonate import gather, handler, resonate, sleep  # noqa: F401


@resonate
def lookup(word: str):
    return f"{word} means something"


@resonate
async def define(words: list):
    found = await gather(lookup.rpc(w) for w in words)
    return {"defined": found}
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
    monkeypatch.delenv("ROUTES_APP", raising=False)
    monkeypatch.delenv("ROUTES_WORKERS", raising=False)
    monkeypatch.delenv("ROUTES_ACCOUNT", raising=False)
    monkeypatch.syspath_prepend(str(ROOT))          # the package, as if installed

    before_registry, before_targets = dict(REGISTRY), dict(TARGETS)
    # One container builds one service, at the first request, and keeps it.
    # That is right in production and wrong across tests: a service built
    # from an earlier test's environment would answer this one.
    app.ROUTES = None
    local.reset()
    yield tmp_path
    app.ROUTES = None
    REGISTRY.clear(), REGISTRY.update(before_registry)
    TARGETS.clear(), TARGETS.update(before_targets)


def serve(tmp_path):
    """The user's app, loaded and answering.

    The `/ready` call is not a health check here: the service is built on
    the first request, not at import, so nothing is wired until one
    arrives. A test that asserted the routing table without making a
    request would be asserting an empty one.
    """
    import functions_framework

    client = functions_framework.create_app(
        "handler", str(tmp_path / "main.py")).test_client()
    assert client.get("/ready").status_code == 200
    return client


def test_the_user_writes_one_file(user_repo):
    """Two files in the repository, and only one of them is theirs."""
    assert sorted(p.name for p in user_repo.iterdir()) == ["main.py", "requirements.txt"]
    assert user_repo.joinpath("requirements.txt").read_text().strip() == "resonate"


def test_one_import_is_the_whole_of_the_wiring(user_repo):
    """No app object, no framework to start, no decorator on the entry
    point. `--function handler` wants a module-level name and the import
    puts one there."""
    serve(user_repo)


def test_the_functions_register_with_nothing_naming_them(user_repo):
    """`ROUTES_APP` existed because the deployed container's entry point was
    ours and the user's code was somewhere else. When the entry point is
    their file, importing it is the registration."""
    assert "ROUTES_APP" not in os.environ
    serve(user_repo)
    assert {("lookup", 0), ("define", 0)} <= set(REGISTRY), sorted(REGISTRY)


def test_rpc_routes_itself_with_no_table(user_repo):
    """One service running everything is the shape a user starts in, and in
    that shape the routing table is derivable from `BASE_URL`."""
    assert "ROUTES_WORKERS" not in os.environ
    serve(user_repo)
    assert TARGETS["lookup"] == f"{BASE}/execute"
    assert TARGETS["define"] == f"{BASE}/execute"


def test_a_split_deployment_still_overrides(user_repo, monkeypatch):
    """Naming one function says where it lives without saying anything
    about the rest, which is what makes the default safe to have."""
    monkeypatch.setenv("ROUTES_WORKERS", json.dumps(
        {"lookup": "https://lookup-svc.a.run.app/execute"}))
    serve(user_repo)
    assert TARGETS["lookup"] == "https://lookup-svc.a.run.app/execute"
    assert TARGETS["define"] == f"{BASE}/execute", "the default was lost"


def test_a_whole_run_from_a_file_a_user_wrote(user_repo):
    """The claim, end to end: their code, their entry point, a durable run."""
    client = serve(user_repo)
    started = client.post("/", json={"kind": "promise.create", "data": {
        "id": "define.1", "timeoutAt": local.CLOCK() + 3_600_000,
        "param": {"data": dumps({"f": "define", "a": [["ping", "pong"]]}).data},
        "tags": {TAG_TARGET: f"{BASE}/execute"}}})
    assert started.status_code == 200, started.get_json()

    # Play the part Cloud Tasks plays: deliver what the queue is holding.
    for _ in range(200):
        delivery = local.QUEUE.take(local.CLOCK())
        if delivery is None:
            break
        path = delivery.url[len(BASE):] if delivery.url.startswith(BASE) else delivery.url
        answer = client.post(path, json=delivery.body or {})
        (local.QUEUE.ack if answer.status_code < 400 else local.QUEUE.nack)(
            delivery, local.CLOCK())

    found = local.STORE.get(doc_key("define.1"))
    assert found, "nothing was ever written"
    root = decode(found[0].encode(), "define.1").get("define.1").promise
    assert root.state == "resolved", root.state
    assert json.loads(root.value.data) == {
        "defined": ["ping means something", "pong means something"]}


def test_the_unused_looking_import_is_load_bearing(user_repo):
    """A linter that strips unused imports deletes the service's entry
    point. `handler` is never referenced in the file, which is exactly what
    an autofixer looks for, so the failure is asserted rather than trusted
    to a comment."""
    stripped = APP.replace(
        "from resonate import gather, handler, resonate, sleep  # noqa: F401",
        "from resonate import gather, resonate, sleep")
    (user_repo / "main.py").write_text(stripped)
    with pytest.raises(Exception) as caught:
        serve(user_repo)
    assert "handler" in str(caught.value), caught.value


def test_the_public_surface_is_small_and_deliberate():
    """Everything else is the engine. A user reaching past this list is a
    gap in it, not a clever workaround."""
    assert set(resonate.__all__) == {
        "handler", "resonate", "gather", "sleep", "Failed", "Durable"}
    for name in resonate.__all__:
        assert hasattr(resonate, name), name


def test_the_example_this_project_deploys_is_the_same_shape():
    """`main.py` at the root of this repository is a user application by
    these rules, not a privileged one. If it ever needs something a user
    could not write, the story has quietly stopped being true."""
    src = (ROOT / "main.py").read_text()
    assert "from resonate import" in src
    assert "handler" in src
    for private in ("from resonate.sdk", "from resonate.app", "from resonate.engine",
                    "from resonate.kernel", "import resonate.sdk"):
        assert private not in src, f"the example reaches into {private}"
