"""What the container gets, against what the code imports.

The suite ran green for the whole life of this project with no
`requirements.txt` in it. Every test imported `google.cloud.storage` from
the machine it ran on, so nothing noticed that the file the buildpack
installs from did not exist -- the service would have built and then died
at its first import. A green suite implying a working deploy is the claim
this file exists to make true.

The check is not "are these four names present". It is: every third-party
module production code imports must be reachable from what
`requirements.txt` names, directly or as a dependency of something it
names. That catches a new import nobody declared, which is the same bug
wearing a different hat.

Resolution is by the file the module actually comes from, not by its
top-level name. `google` is a namespace package: `google.cloud.storage`
and `google.cloud.tasks_v2` share a root but come from different
distributions, so a check on roots alone says `google-cloud-tasks` covers
both and a dropped `google-cloud-storage` sails through. The first version
of this file had exactly that hole, and it was found by deleting a line
from `requirements.txt` and watching the test pass.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

#: Production is everything the container runs. `test/` is not in the image
#: at all (see `.gcloudignore`), and `conftest.py` is pytest's.
EXCLUDED = {"conftest.py"}


def production_files() -> list[Path]:
    return sorted(p for p in list(ROOT.glob("*.py")) + list(ROOT.glob("spec/*.py"))
                  if p.name not in EXCLUDED)


def imported_modules(path: Path) -> set[tuple[str, ...]]:
    """Every import in a file, as the names to try, most specific first.

    `from google.cloud import storage` has to be looked up as
    `google.cloud.storage`: `google.cloud` is a namespace package that owns
    no file and so belongs to no distribution. But `from ports import
    Conflict` imports a name, not a module, so the less specific form has
    to remain a candidate.
    """
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {(a.name,) for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found |= {(f"{node.module}.{a.name}", node.module) for a in node.names}
    return found


def declared() -> set[str]:
    """The distributions `requirements.txt` names, normalised."""
    text = (ROOT / "requirements.txt").read_text()
    names = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(line.split("[")[0].split("==")[0].split(">")[0].split("<")[0]
                      .strip().lower().replace("_", "-"))
    return names


def reachable(seed: set[str]) -> set[str]:
    """Everything pip would install for `seed`: the transitive closure."""
    seen, todo = set(), list(seed)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for req in requires:
            # "google-auth (>=2.0) ; extra == 'foo'" -- skip the extras.
            if ";" in req and "extra ==" in req.split(";", 1)[1]:
                continue
            dep = req.split(";")[0].split("(")[0].split("[")[0]
            for sep in ("<", ">", "=", "!", "~", " "):
                dep = dep.split(sep)[0]
            if dep.strip():
                todo.append(dep.strip().lower().replace("_", "-"))
    return seen


def test_a_production_requirements_file_exists():
    """The buildpack installs from this file and no other."""
    assert (ROOT / "requirements.txt").is_file(), (
        "no requirements.txt: the container would build without the "
        "Google libraries and fail at its first import")


def test_no_test_tool_is_named_in_it():
    """Production importing a test tool is the bug; a line here is the symptom."""
    assert not (declared() & {"pytest", "hypothesis", "jsonschema", "mypy"})


@pytest.mark.skipif(not (ROOT / "requirements.txt").is_file(), reason="no requirements.txt")
def test_everything_production_imports_is_installed_by_it():
    have = reachable(declared())
    owners = _owner_of_file()

    missing: dict[str, set[str]] = {}
    for path in production_files():
        for candidates in imported_modules(path):
            root = candidates[0].split(".")[0]
            if root in sys.stdlib_module_names or (ROOT / f"{root}.py").exists() \
                    or (ROOT / root).is_dir():
                continue                      # stdlib, or our own module
            owners_found = [owning_distribution(c, owners) for c in candidates]
            if any(o in have for o in owners_found if o):
                continue
            seen = next((o for o in owners_found if o), None)
            missing.setdefault(f"{candidates[0]} ({seen or 'not installed'})",
                               set()).add(path.name)
    assert not missing, (
        "production code imports modules the container will not have: "
        + "; ".join(f"{k} in {', '.join(sorted(v))}" for k, v in sorted(missing.items())))


def _owner_of_file() -> dict[str, str]:
    """absolute path -> the distribution that installed it.

    The only way to tell `google.cloud.storage` from `google.cloud.tasks_v2`,
    since the two share every name above the leaf.
    """
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata["Name"] or "").lower().replace("_", "-")
        for f in dist.files or ():
            try:
                out[str(f.locate().resolve())] = name
            except Exception:
                continue
    return out


def owning_distribution(module: str, owners: dict[str, str]) -> str | None:
    """Which distribution provides this module, by where its file is."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or not spec.origin or spec.origin == "built-in":
        return None
    return owners.get(str(Path(spec.origin).resolve()))


def test_the_function_runtime_is_named_even_though_nothing_imports_it():
    """`functions-framework` is the harness the buildpack runs `handler`
    with, not a library the code calls. No production module imports it, so
    the check above is structurally blind to it, and a container without it
    builds fine and then has no way to serve a request.

    The same reasoning is why dropping `google-auth` from
    `requirements.txt` does not fail the check above and should not:
    `google-cloud-storage` depends on it, so pip installs it regardless.
    Naming it is honesty about a direct import, not a load-bearing line.
    """
    assert "functions-framework" in declared()


def test_the_entry_point_the_buildpack_looks_for_exists():
    """`main.py` is not a convenience. Google's Python buildpack fails with
    `MissingSourceException` without it, which is a deploy-time failure no
    amount of local testing reaches."""
    assert (ROOT / "main.py").is_file()
    assert "handler" in (ROOT / "main.py").read_text()


#: Every environment variable `functions-framework` reads for itself,
#: harvested from the installed package rather than from memory. A name in
#: here means the runtime owns it, whatever we might want it for.
RUNTIME_OWNED = {
    "CLOUD_RUN_TIMEOUT_SECONDS", "ENTRY_POINT", "FUNCTION_NAME",
    "FUNCTION_SOURCE", "FUNCTION_TARGET", "FUNCTION_TRIGGER_TYPE",
    "GUNICORN_LOG_LEVEL", "HTTP_FUNCTION_EXECUTION_ID", "K_SERVICE",
    "LOG_EXECUTION_ID", "THREADED_TIMEOUT_ENABLED", "THREADS", "WORKERS",
}

#: Ones the service reads on purpose because the platform sets them.
BORROWED = {"K_REVISION"}


def configured_names() -> set[str]:
    """Every environment variable `app.py` reads."""
    names: set[str] = set()
    tree = ast.parse((ROOT / "app.py").read_text())
    for node in ast.walk(tree):
        # os.environ.get("X") / os.environ.get("X", default)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("get", "pop") and node.args \
                and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            if "environ" in ast.dump(node.func.value):
                names.add(node.args[0].value)
        # os.environ["X"]
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str) and "environ" in ast.dump(node.value):
            names.add(node.slice.value)
    return names


def test_no_configuration_name_collides_with_the_runtime():
    """The bug this file could not have caught, so it catches the next one.

    `WORKERS` was the routing map until a Cloud Run deploy refused to start:
    `functions-framework` reads it as gunicorn's worker count and raises
    `ValueError: invalid literal for int()` on our JSON, before the container
    listens on its port. Every local test builds the app with `create_app`
    and never starts gunicorn, so nothing saw it -- the collision is only
    visible on the real serving path.

    A name is not testable into safety here; it has to stay out of the
    runtime's namespace. So this asserts the namespaces are disjoint.
    """
    clash = (configured_names() & RUNTIME_OWNED) - BORROWED
    assert not clash, (
        "app.py reads environment variables functions-framework owns: "
        + ", ".join(sorted(clash))
        + " -- the container will not start. Prefix them, as ROUTES_* does.")


def test_the_check_can_see_the_names_at_all():
    """A set-intersection test passes just as well against an empty set,
    which would make the check above a decoration."""
    found = configured_names()
    assert {"BUCKET", "PROJECT", "QUEUE", "ROUTES_WORKERS"} <= found, found
