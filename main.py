"""Entry point for the frozen executable.

PyInstaller runs its entry script as top-level `__main__`, which breaks the relative
imports in `meglaping/__main__.py`, so the build targets this file instead.
"""

import sys

from meglaping.cli import main

if __name__ == "__main__":
    sys.exit(main())
