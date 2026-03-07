# models/market.py
from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class BarModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    t: datetime  # 時間
    o: float     # 開盤
    h: float     # 最高
    l: float     # 最低
    c: float     # 收盤
    v: int       # 成交量

class ContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    description: Optional[str] = None
    tickSize: float
    tickValue: float
    symbolId: str
    activeContract: bool = True

    @property
    def point_value(self) -> float:
        """根據 API 提供的 Tick 資訊自動計算每點價值"""
        return self.tickValue / self.tickSize if self.tickSize > 0 else 0.0