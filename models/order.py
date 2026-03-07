# models/order.py
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime

class OrderResponse(BaseModel):
    orderId: Optional[int] = None
    success: bool
    errorCode: int
    errorMessage: Optional[str] = None

class PositionModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    contractId: str
    symbolName: str
    side: int  
    size: int
    averagePrice: float
    currentPrice: float = 0.0
    pointValue: float = 2.0  # 預設值，由 Monitor 自動從合約資訊補齊

    @property
    def unrealized_pnl_usd(self) -> float:
        # 如果現價或均價為 0，代表數據尚未準備好，返回 0 避免驚悚數字
        if self.currentPrice <= 0 or self.averagePrice <= 0:
            return 0.0
            
        diff = (self.currentPrice - self.averagePrice) if self.side == 0 else (self.averagePrice - self.currentPrice)
        return diff * self.size * self.pointValue

class TradeModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    accountId: int
    contractId: str
    price: float
    size: int
    side: int
    profitAndLoss: Optional[float] = 0.0 # 允許 API 回傳 null
    fees: Optional[float] = 0.0
    creationTimestamp: Optional[datetime] = None
    orderId: int