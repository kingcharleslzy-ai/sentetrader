"""Replay API input contracts; Sidecar still validates historical availability."""
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .instruments import SYMBOL_PATTERN, foreign_product_of
from .trading_costs import FeeSchedule

# 回放只有国内期货的历史逐笔（快期回放）；外盘合约到路由里给中文原因拒绝，不在格式这一层报 422 列表。
FOREIGN_REPLAY_REFUSAL = "外盘暂不支持回放：回放读的是国内期货的历史逐笔，外盘品种没有。请换一个国内合约。"


class ReplayCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=40)
    tradingDay: date
    initialBalance: float = Field(ge=100000, le=100000000, strict=True)
    # The period the client is displaying when it starts the replay; the sidecar
    # reaches ready once it is in and backfills the rest in the background. Every
    # display period the chart can sit on (shared_market.DURATIONS) is accepted.
    defaultDurationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] | None = None
    feeSchedule: FeeSchedule | None = None

    @field_validator("symbol")
    @classmethod
    def _concrete_contract(cls, value: str) -> str:
        # 国内要具体合约（SHFE.rb2610）；外盘代码放过格式，由路由用 FOREIGN_REPLAY_REFUSAL 拒绝。
        if foreign_product_of(value) is None and not re.fullmatch(r"[A-Za-z]+\.[A-Za-z]+\d{3,4}", value):
            raise ValueError("回放要一个具体合约，如 SHFE.rb2610")
        return value


class ReplayControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["play", "pause", "step", "speed", "pause_entries", "resume_entries", "finish", "close"]
    speed: Literal[1, 2, 5, 10] | None = None
