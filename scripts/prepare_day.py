#!/usr/bin/env python3
"""One-shot runner for trading-day operational preparation."""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.service_config import SERVICE_CONFIG
from logconfig import setup_logging
from services.operations.day_prep import DayPrepService
from utils.run_control import allow_run_today


def main() -> int:
    setup_logging(log_file=SERVICE_CONFIG.day_prep.log_file)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "day_prep"):
        return 0

    try:
        DayPrepService().prepare()
    except Exception:
        logger.exception("DAY_PREP_FAILED")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
