# SPDX-License-Identifier: Apache-2.0
"""Passenger entry point for deployments where the code is NOT the docroot.

cPanel's Setup Python App expects ``passenger_wsgi.py`` to sit in the domain
document root - that is how a working app on this host is laid out. When the
code lives elsewhere, e.g. ``~/repositories/pytracking``, place THIS file in
the document root and set:

    Application startup file   passenger_wsgi.py
    Application Entry point    application
    PassengerAppRoot           /path/to/the/document/root

The real module is loaded from the repository by absolute path under a
distinct name (``pytracking_wsgi``). Importing it as ``passenger_wsgi`` would
resolve to this very file, since Passenger loads it under that name.

Set ``PYTRACKING_APP_DIR`` to override the location below.
"""

import importlib.util
import os
import sys

APP_DIR = os.environ.get(
    "PYTRACKING_APP_DIR",
    os.path.expanduser("~/repositories/pytracking"),
)
APP_FILE = os.path.join(APP_DIR, "passenger_wsgi.py")

if not os.path.isfile(APP_FILE):
    raise RuntimeError(
        "pytracking: no passenger_wsgi.py in {0!r}. Set PYTRACKING_APP_DIR to "
        "the directory holding the app, or move the code into the document "
        "root.".format(APP_DIR)
    )

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

_spec = importlib.util.spec_from_file_location("pytracking_wsgi", APP_FILE)
_module = importlib.util.module_from_spec(_spec)
sys.modules["pytracking_wsgi"] = _module
_spec.loader.exec_module(_module)

application = _module.application
