"""Entry point: ``python -m snowdesk`` / ``uv run snowdesk``."""

from __future__ import annotations

import sys

from snowdesk.app import main

if __name__ == "__main__":
    sys.exit(main())
