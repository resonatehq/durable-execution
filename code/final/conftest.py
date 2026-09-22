"""Make the modules under test importable from `test/`.

pytest puts a test file's own directory on `sys.path`, which for these is
`test/`. The things they import — `engine`, `store_mem`, `spec` — live one
level up, so that is what goes on the path. A conftest at the root is also
what makes `code/final` the rootdir, so `python -m pytest` from here finds
everything.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
