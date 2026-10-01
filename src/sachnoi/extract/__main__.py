"""`python -m sachnoi.extract` entrypoint.

Kept in its own file so the package is runnable without agent B touching
`cli.py` -- CONTRACT.md reserves that file for the other machine.
"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
