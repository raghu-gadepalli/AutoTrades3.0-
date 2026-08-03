# config.py

from datetime import date, timedelta
import os
from typing import Dict
from tenacity import stop_after_attempt, wait_fixed, wait_exponential


# -----------------------
# 1. Global application settings
# -----------------------
class AppConfig:
    """Secrets, DB URIs & global defaults."""

    SECRET_KEY = "AUTOTRADES2.0"
    DATA_USER = "VZS807"
    DEMO_USER = "AT1234"
    DEMO_SESSION_HOURS = 1
    SIGNAL_SOURCE = "autotrades"
    OPERATORS = ["DR1812", "VZS807"]
    ADMINS = ["DR1812", "VZS807"]

    PERMANENT_SESSION_LIFETIME = timedelta(days=1)
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_REFRESH_EACH_REQUEST = False

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = os.getenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    SQLALCHEMY_BINDS: Dict[str, str] = {
        "trades": os.getenv(
            "TRADE_DATABASE_URI",
            "mysql+mysqlconnector://autotrades:Autotrades001%23@88.222.212.231/backtest",
        ),
    }
# -----------------------
# 2. Logging flags
# -----------------------
class LoggingConfig:
    """Global logging level, default file, and console toggle."""

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE = os.getenv("LOG_FILE", "/var/log/autotrades/app.log")
    LOG_TO_CONSOLE = os.getenv("LOG_TO_CONSOLE", "True").lower() in ("1", "true", "yes")


# -----------------------
# 3. Retry policies
# -----------------------
class RetryConfig:
    """Tenacity stop / wait rules for various use cases."""

    HISTORICAL = {
        "stop": stop_after_attempt(5),
        "wait": wait_exponential(multiplier=1, max=10),
    }
    ORDER = {
        "stop": stop_after_attempt(4),
        "wait": wait_fixed(2),
    }
    BASIC = {
        "stop": stop_after_attempt(2),
        "wait": wait_fixed(1),
    }


# -----------------------
# 4. Trade gate config
# -----------------------


class TelemetryConfig:
    """
    Compact decision trace controls.
    Start with JSON/meta traces before adding a full audit table.
    """

    ENABLE_GATE_TRACE = True
    ENABLE_STRUCTURE_TRACE = True
    ENABLE_LIFECYCLE_TRACE = True

    STORE_LATEST_TRACE_IN_META = True
    STORE_AUDIT_TABLE = False

    MAX_TRACE_REASONS = 20


# -----------------------
# 5. F&O expiry config
# -----------------------
class FoConfig:
    """Monthly F&O contract selection.

    FRONT_EXPIRY is the monthly expiry the runtime should trade.
    Update this once per month after rollover.

    LOAD_EXPIRY_COUNT controls how many monthly expiries generate_futopt
    stores into symbols, starting from FRONT_EXPIRY. Keeping 3 months avoids
    regenerating symbols only because the front month changes.
    """

    FRONT_EXPIRY = date(2026, 8, 25)
    LOAD_EXPIRY_COUNT = 3


# -----------------------
# 6. Per-service runtime settings
# -----------------------
class Services:
    """Global runtime controls that are still shared across services."""

    RUN_CONTROL = {
        "tz": "Asia/Kolkata",
        "holidays": [
            "2026-01-15",
            "2026-01-26",
            "2026-03-03",
            "2026-03-26",
            "2026-03-31",
            "2026-04-03",
            "2026-04-14",
            "2026-05-01",
            "2026-05-28",
            "2026-06-26",
            "2026-09-14",
            "2026-10-02",
            "2026-10-20",
            "2026-11-10",
            "2026-11-24",
            "2026-12-25",
        ],
        "blackout_dates": [],
        "whitelist_dates": [
            "2026-02-01",
        ],
    }

