"""Make the modules under test importable from `test/`.

pytest puts a test file's own directory on `sys.path`, which for these is
`test/`. What they import — the `resonate` package, and the `main.py`
beside it that stands in for a user's application — lives one level up, so
that is what goes on the path. In a user's repository the package is
installed and this file does not exist; here it is a sibling directory,
which is the same import either way. A conftest at the root is also what
makes `code/final` the rootdir, so `python -m pytest` from here finds
everything.
"""

import os
import sys
from pathlib import Path

# The examples end in `handler = serve()`, which builds the service when the
# file is imported. Without this, importing one would reach for a bucket.
os.environ.setdefault("SIMULATED", "1")

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
