from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ReplayConfig(BaseModel):
    execution_price_source: Literal["1m_candle", "snapshot"] = "snapshot"


REPLAY_CONFIG = ReplayConfig()
