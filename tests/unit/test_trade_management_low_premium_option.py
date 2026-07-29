from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import unittest

from configs.monitor_config import MONITOR_CONFIG
from services.trade.monitor.trademon_helper import TradeMonHelper


ASOF = datetime(2026, 7, 29, 11, 37, 7)


class LowPremiumOptionTradeManagementTests(unittest.TestCase):
    def test_low_premium_bought_option_gets_positive_capped_stop(self):
        tm = TradeMonHelper.initialize_trade_management(
            side="BUY",
            instrument_type="PE",
            entry_price=Decimal("3.10"),
            underlying_atr=Decimal("7.020420876484668"),
            asof_time=ASOF,
            signal_setup_label="REVERSAL",
        )

        cap_pct = Decimal(str(MONITOR_CONFIG.trade_management.setup_option_risk_cap_pct))
        expected_stop = Decimal("3.10") * (Decimal("1") - cap_pct)

        self.assertGreater(Decimal(str(tm["current_stop_price"])), Decimal("0"))
        self.assertEqual(expected_stop, Decimal(str(tm["current_stop_price"])))
        self.assertEqual("OPTION_PREMIUM_RISK_CAP", tm["initial_stop_source"])
        self.assertEqual(
            "initial_stop_capped_to_positive_option_premium",
            tm["initial_stop_reason"],
        )

    def test_fill_rebase_keeps_low_premium_option_stop_positive_and_normalizable(self):
        planned = TradeMonHelper.initialize_trade_management(
            side="BUY",
            instrument_type="PE",
            entry_price=Decimal("3.10"),
            underlying_atr=Decimal("7.020420876484668"),
            asof_time=ASOF,
            signal_setup_label="REVERSAL",
        )

        rebased = TradeMonHelper.rebase_trade_management_after_fill(
            raw=planned,
            side="BUY",
            instrument_type="PE",
            planned_entry_price=Decimal("3.10"),
            executed_entry_price=Decimal("3.70"),
            asof_time=ASOF,
        )

        cap_pct = Decimal(str(MONITOR_CONFIG.trade_management.setup_option_risk_cap_pct))
        expected_stop = Decimal("3.70") * (Decimal("1") - cap_pct)
        self.assertEqual(expected_stop, Decimal(str(rebased["current_stop_price"])))
        self.assertGreater(Decimal(str(rebased["current_stop_price"])), Decimal("0"))

        normalized = TradeMonHelper.normalize_trade_management(
            raw=rebased,
            side="BUY",
            instrument_type="PE",
            entry_price=Decimal("3.70"),
            underlying_atr=Decimal("7.020420876484668"),
            asof_time=ASOF,
        )
        self.assertGreater(Decimal(str(normalized["current_stop_price"])), Decimal("0"))
        self.assertGreater(Decimal(str(normalized["current_target_price"])), Decimal("0"))

    def test_normal_option_stop_is_not_changed(self):
        tm = TradeMonHelper.initialize_trade_management(
            side="BUY",
            instrument_type="CE",
            entry_price=Decimal("100"),
            underlying_atr=Decimal("10"),
            asof_time=ASOF,
        )
        self.assertEqual("ATR_MULTIPLE", tm["initial_stop_source"])
        self.assertEqual(Decimal("88.75"), Decimal(str(tm["current_stop_price"])))


if __name__ == "__main__":
    unittest.main()
