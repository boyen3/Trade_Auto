import os

def create_project_v2():
    # 1. 定義資料夾結構
    folders = [
        "core",
        "models",
        "services",
        "strategies",
        "tests",
        "utils"
    ]

    # 2. 定義檔案內容映射 (包含我們最新的實測代碼)
    files = {
        # --- 環境配置 ---
        ".env": "TOPSTEP_API_KEY=your_key_here\nDEFAULT_ACCOUNT_ID=\nBASE_URL=https://api.topstepx.com",
        "requirements.txt": "httpx\npydantic\npython-dotenv\npandas",
        
        # --- Core ---
        "core/constants.py": """from enum import IntEnum

class OrderSide(IntEnum):
    BUY = 0
    SELL = 1

class OrderType(IntEnum):
    MARKET = 1
    LIMIT = 2
    STOP = 3

class BracketType(IntEnum):
    TAKE_PROFIT = 1
    STOP_LOSS = 4

class BarUnit(IntEnum):
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
""",
        "core/config.py": """import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    API_KEY = os.getenv('TOPSTEP_API_KEY')
    BASE_URL = os.getenv('BASE_URL', 'https://api.topstepx.com')
    ACCOUNT_ID = os.getenv('DEFAULT_ACCOUNT_ID')
""",

        # --- Models ---
        "models/market.py": """from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class BarModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: int

class ContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    tickSize: float
    tickValue: float
    symbolId: str

    @property
    def point_value(self) -> float:
        return self.tickValue / self.tickSize if self.tickSize > 0 else 0.0
""",
        "models/order.py": """from pydantic import BaseModel, ConfigDict
from typing import Optional, List
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
    pointValue: float = 2.0 

    @property
    def unrealized_pnl_usd(self) -> float:
        if self.currentPrice <= 0 or self.averagePrice <= 0: return 0.0
        diff = (self.currentPrice - self.averagePrice) if self.side == 0 else (self.averagePrice - self.currentPrice)
        return diff * self.size * self.pointValue

class TradeModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    profitAndLoss: Optional[float] = 0.0
    fees: Optional[float] = 0.0
    creationTimestamp: datetime
    orderId: int
""",

        # --- Strategies (基類) ---
        "strategies/base_strategy.py": """from abc import ABC, abstractmethod
import pandas as pd
from services.trading import TradingService
from services.market_data import MarketDataService

class BaseStrategy(ABC):
    def __init__(self, trading_service: TradingService, market_service: MarketDataService):
        self.ts = trading_service
        self.ms = market_service
        self.account_id = None
        self.contract_id = None

    @abstractmethod
    async def on_bar(self, df: pd.DataFrame):
        pass
""",
    }

    print("🏗️  正在重建 ProjectX V2 生態系統...")

    # 執行建立
    for folder in folders:
        os.makedirs(folder, exist_ok=True)
        init_path = os.path.join(folder, "__init__.py")
        if not os.path.exists(init_path):
            open(init_path, "w").close()

    for path, content in files.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.strip())
        print(f"  [+] {path}")

    print("\n✨ 重建完成！所有實測修正已整合。")

if __name__ == "__main__":
    create_project_v2()