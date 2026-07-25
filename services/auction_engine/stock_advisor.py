"""Compatibility import for the decoupled signal-time StockAdvisor.

New code must import ``services.signals.stock_advisor.StockAdvisor``.
"""
from services.signals.stock_advisor import StockAdvisor

__all__ = ["StockAdvisor"]
