# models/account.py
from pydantic import BaseModel, Field
from typing import List, Optional

class AccountModel(BaseModel):
    """
    帳戶資料模型
    確保 API 數據能穩定轉換為系統內部對象。
    """
    id: int
    name: str
    balance: float
    
    # 使用 alias 映射 API 的 realizedPnL，並給予預設值以防驗證失敗
    realized_pnl: float = Field(alias="realizedPnL", default=0.0)
    
    # 賦予預設值而非設為 Optional，確保底層邏輯調用時始終有布林值
    canTrade: bool = Field(default=True)
    isVisible: bool = Field(default=True)

    class Config:
        # 允許在程式碼中使用蛇形命名 (realized_pnl) 進行賦值
        populate_by_name = True

class AccountSearchResponse(BaseModel):
    accounts: List[AccountModel]