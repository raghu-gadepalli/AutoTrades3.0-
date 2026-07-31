#!/usr/bin/env python3
"""Manual functionality entry point for enabled-universe policy review.

Examples:
    python tests/functionality/test_filter_stock_universe.py
    python tests/functionality/test_filter_stock_universe.py --symbols LT,INFY
    python tests/functionality/test_filter_stock_universe.py --apply
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from operations.filter_stock_universe import main


if __name__ == "__main__":
    raise SystemExit(main())
