"""Load an example the way the platform loads it.

Every example is a `main.py`, because that is the name Google's buildpack
insists on, and there are two of them. Nothing can be `import main`, so
each is loaded from its path under a name of its own -- which is also what
`functions_framework.create_app` does, so the tests and the deployment
reach these modules the same way.

The directory goes on `sys.path` for the duration, because an example with
more than one file imports its own siblings (`from tools import ...`) just
as it would in the user's own repository.
"""

from __future__ import annotations

import sys
from importlib import util
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"


def path_to(name: str) -> Path:
    return EXAMPLES / name / "main.py"


def load(name: str):
    """The example's `main.py`, imported, under the module name `name`."""
    directory = str(EXAMPLES / name)
    added = directory not in sys.path
    if added:
        sys.path.insert(0, directory)
    try:
        module_name = name.replace("-", "_")
        spec = util.spec_from_file_location(module_name, path_to(name))
        module = util.module_from_spec(spec)
        # Registered before exec so that a module importing itself by name
        # gets this one, and removed after so the next load is a fresh one.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        return module
    finally:
        if added:
            sys.path.remove(directory)
