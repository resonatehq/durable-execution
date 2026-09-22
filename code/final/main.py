"""The name the platform insists on.

Google's Python buildpack looks for `main.py` at the root and fails with
`MissingSourceException` otherwise. This file exists because running

    functions-framework --target=handler

against `app.py` said exactly that, which is the kind of thing that is
cheap to find on a laptop and expensive to find on a first deploy.

The service is `app.py`. This is one line so that the file a reader opens
looking for the service is not the one the build system needs.
"""

from app import handler

__all__ = ["handler"]
