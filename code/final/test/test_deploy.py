"""What the container gets, against what the code imports.

Every test imports its libraries from the machine it runs on, so nothing
else notices when the file the buildpack installs from is missing or short
-- the service would build and then die at its first import. A green suite
implying a working deploy is the claim this file exists to make true.

The check is not "are these four names present". It is: every third-party
module production code imports must be reachable from what
`requirements.txt` names, directly or as a dependency of something it
names. That catches a new import nobody declared, which is the same bug
wearing a different hat.

Resolution is by the file the module actually comes from, not by its
top-level name. `google` is a namespace package: `google.cloud.storage`
and `google.cloud.tasks_v2` share a root but come from different
distributions, so a check on roots alone says `google-cloud-tasks` covers
both and a dropped `google-cloud-storage` sails through.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

#: Production is the package. The applications under `examples/` are not
#: part of it -- each ships its own `requirements.txt` and is checked
#: against that instead, by `test_each_example_declares_what_it_imports`,
#: because an example that needed `anthropic` would say so in its own file
#: and not in the engine's. Relative imports inside the package are skipped
#: by `imported_modules`: a `from .kernel import` names no distribution and
#: never could.
def production_files() -> list[Path]:
    return sorted(ROOT.glob("resonate/**/*.py"))


EXAMPLES = sorted(p for p in (ROOT / "examples").iterdir() if p.is_dir())


def imported_modules(path: Path) -> set[tuple[str, ...]]:
    """Every import in a file, as the names to try, most specific first.

    `from google.cloud import storage` has to be looked up as
    `google.cloud.storage`: `google.cloud` is a namespace package that owns
    no file and so belongs to no distribution. But `from resonate.errors import
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
    """The distributions the engine's `requirements.txt` names."""
    return named_in(ROOT / "requirements.txt")


def named_in(requirements: Path) -> set[str]:
    """The distributions a requirements file names, normalised."""
    text = requirements.read_text()
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


#: What the engine is allowed to need. Pydantic is not deferred anywhere and
#: is not worth deferring; everything else in `requirements.txt` belongs to
#: the runtime or to Google, and is imported inside the method that needs it.
DEFERRED = ("flask", "google", "functions_framework")

#: Refuse those three, then import the package and the simulators.
WITHOUT_THEM = """
import sys
BLOCKED = %r
class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(name + " is not installed")
        return None
sys.meta_path.insert(0, Refuse())
import resonate
import resonate.kernel, resonate.engine
import resonate.testing.explore, resonate.testing.sim
""" % (DEFERRED,)


def test_the_engine_imports_without_the_things_only_a_deployment_needs():
    """`resonate/__init__.py` re-exports `serve`, so every import of this
    package reaches `server.py`. While that file imported Flask at the top,
    `import resonate.kernel` needed a web framework -- and so did every
    simulation, every exploration and the whole suite, on a machine that had
    only what the kernel actually uses. `store_gcp` and `queue_gcp` had
    always deferred their Google imports for this reason; `server.py` was
    the one that had not, and nothing noticed because the machines that run
    the tests happen to have Flask.
    """
    done = subprocess.run([sys.executable, "-c", WITHOUT_THEM],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_that_check_can_fail():
    """The blocker has to actually block, or the test above passes on a
    machine with Flask installed and says nothing."""
    done = subprocess.run([sys.executable, "-c", WITHOUT_THEM + "\nimport flask\n"],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode != 0 and "flask" in done.stderr


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


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_the_entry_point_the_buildpack_looks_for_exists(example):
    """`main.py` is not a convenience. Google's Python buildpack fails with
    `MissingSourceException` without it, which is a deploy-time failure no
    amount of local testing reaches. Every deployable thing in this
    repository is an example directory, so every one of them needs it."""
    assert (example / "main.py").is_file(), example.name
    src = (example / "main.py").read_text()
    assert "handler = serve()" in src, f"{example.name} has no entry point to deploy"
    assert "from resonate import" in src, f"{example.name} does not use the package"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_each_example_declares_what_it_imports(example):
    """An example is a user application, so it answers for its own
    dependencies. `travel-agent` reaches for `anthropic`; the engine does
    not, and neither file should have to carry the other's list."""
    requirements = example / "requirements.txt"
    assert requirements.is_file(), f"{example.name} has no requirements.txt"

    names = named_in(requirements) | {"resonate"}
    # Declared *or* installed. The engine's own dependencies are always
    # installed here, so they resolve by the file they come from -- which is
    # the check that catches a namespace package. An example may name one
    # that is not installed in this environment (`anthropic` is optional
    # even for the example that uses it), and naming it is the whole of
    # what this test asks of it.
    have = reachable(names)
    owners = _owner_of_file()
    siblings = {p.stem for p in example.glob("*.py")}

    missing = {}
    for path in sorted(example.glob("*.py")):
        for candidates in imported_modules(path):
            root = candidates[0].split(".")[0]
            if root in sys.stdlib_module_names or root in siblings or root == "resonate":
                continue
            if root.lower().replace("_", "-") in names:
                continue
            found = [owning_distribution(c, owners) for c in candidates]
            if any(o in have for o in found if o):
                continue
            missing.setdefault(candidates[0], set()).add(path.name)
    assert not missing, (
        f"{example.name} imports what its requirements.txt does not name: "
        + "; ".join(f"{k} in {', '.join(sorted(v))}" for k, v in sorted(missing.items())))


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
BORROWED = {"K_REVISION", "K_SERVICE"}


def configured_names() -> set[str]:
    """Every environment variable `config.py` reads, as `env.get("X")` or
    `env["X"]`."""
    names: set[str] = set()
    tree = ast.parse((ROOT / "resonate" / "config.py").read_text())
    for node in ast.walk(tree):
        # env.get("X") / env.get("X", default) / env["X"]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("get", "pop") and node.args \
                and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "env":
                names.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str) \
                and isinstance(node.value, ast.Name) and node.value.id == "env":
            names.add(node.slice.value)
    return names


def test_no_configuration_name_collides_with_the_runtime():
    """`functions-framework` reads names like `WORKERS` for itself (that one
    as gunicorn's worker count), and a clash fails before the container
    listens on its port. Every local test builds the app with `create_app`
    and never starts gunicorn, so the collision is only visible on the real
    serving path.

    A name is not testable into safety here; it has to stay out of the
    runtime's namespace. So this asserts the namespaces are disjoint.
    """
    clash = (configured_names() & RUNTIME_OWNED) - BORROWED
    assert not clash, (
        "config.py reads environment variables functions-framework owns: "
        + ", ".join(sorted(clash))
        + " -- the container will not start. Prefix them, as ROUTES_* does.")


def test_the_check_can_see_the_names_at_all():
    """A set-intersection test passes just as well against an empty set,
    which would make the check above a decoration."""
    found = configured_names()
    assert {"BUCKET", "PROJECT", "QUEUE", "ROUTES_WORKERS"} <= found, found


def test_the_example_the_deployment_runs_is_the_one_in_the_readme():
    """`examples/research-agent/main.py` is both the program a reader copies
    out of the README and the one every test drives, so it has to stay that
    program rather than drift into a second version of it."""
    src = (ROOT / "examples" / "research-agent" / "main.py").read_text()
    for step in ("Plan the searches", "Fan out the searches",
                 "Synthesize the results", "gather(search.rpc(q)"):
        assert step in src, step
