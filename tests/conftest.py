"""Pytest bootstrap: make ex_app/lib importable as plain top-level modules
(``analyze``, ``main``), matching how main.py itself imports them
(``from analyze import ...``) — this repo's ex_app/lib has no __init__.py,
so it is not a package; tests need the same sys.path shape as the running
container (WORKDIR /app, PYTHONPATH implicitly ex_app/lib via main.py's cwd).
"""

import sys
from pathlib import Path

EX_APP_LIB = Path(__file__).resolve().parent.parent / "ex_app" / "lib"
if str(EX_APP_LIB) not in sys.path:
    sys.path.insert(0, str(EX_APP_LIB))
