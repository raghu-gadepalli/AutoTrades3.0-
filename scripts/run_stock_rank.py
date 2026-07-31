#!/usr/bin/env python3
"""Reserved production entry point for the six-minute StockRank service.

The retired first-candle StockScan selector has deliberately been removed.
Keep ``t_run_stock_rank.service`` disabled until the StockRank runner patch
implements completed-snapshot cadence validation and persistence.
"""

from __future__ import annotations

import logging
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from logconfig import setup_logging

LOG_FILE = "/var/www/autotrades/scripts/stock_rank.log"


def main() -> int:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)
    logger.error(
        "StockRank production runner is not implemented in this patch. "
        "Keep t_run_stock_rank.service disabled until the StockRank service patch is applied."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
