from __future__ import annotations

import unittest

from tests.replay_database_guard import require_allowed_replay_database


class ReplayDatabaseGuardTests(unittest.TestCase):
    def test_accepts_exact_unprotected_database(self) -> None:
        self.assertEqual(
            require_allowed_replay_database(
                database_uri="mysql+mysqlconnector://user:pass@localhost/backtest",
                allowed_database="backtest",
            ),
            "backtest",
        )

    def test_rejects_configured_database_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            require_allowed_replay_database(
                database_uri="mysql+mysqlconnector://user:pass@localhost/testing",
                allowed_database="backtest",
            )

    def test_rejects_protected_configured_database(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protected"):
            require_allowed_replay_database(
                database_uri="mysql+mysqlconnector://user:pass@localhost/autotrades",
                allowed_database="backtest",
            )

    def test_rejects_protected_allowed_database(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protected"):
            require_allowed_replay_database(
                database_uri="mysql+mysqlconnector://user:pass@localhost/autotrades",
                allowed_database="autotrades",
            )


if __name__ == "__main__":
    unittest.main()
