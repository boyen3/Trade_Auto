# strategies/base_strategy.py
from abc import ABC, abstractmethod
import pandas as pd
from core.data_hub import DataHub
from core.state_store import StateStore
from utils.logger import logger

class BaseStrategy(ABC):
    def __init__(self, trading_service, market_service):
        """
        初始化策略基底類別
        :param trading_service: 負責執行交易與查詢訂單狀態
        :param market_service: 負責獲取市場數據
        """
        self.ts = trading_service
        self.ms = market_service
        self.account_id = None
        self.contract_id = None
        
        # 初始化持久化組件，讓策略具備跨重啟的記憶能力
        self.store = StateStore()
        self.strategy_name = self.__class__.__name__
        
        # 當前持倉快照，會由 run_step 自動從 DataHub 同步
        self.current_position = None

    def save_memory(self, **kwargs):
        """將策略關鍵變數（例如：進場標記、止損價）存入硬碟"""
        self.store.update_strategy_state(self.strategy_name, kwargs)

    def load_memory(self) -> dict:
        """從硬碟讀取該策略的歷史記憶"""
        return self.store.get_strategy_state(self.strategy_name)

    async def run_step(self):
        """
        策略執行的核心步驟。由事件引擎觸發，負責同步數據並呼叫實體策略邏輯。
        """
        if not self.account_id or not self.contract_id:
            return

        # 1. 從 DataHub 獲取最新 K 線（不消耗 API 次數，解決限流問題）
        df = DataHub.get_bars(self.contract_id)
        if df.empty:
            return

        # 2. 自動從 DataHub 同步最新持倉快照
        self.current_position = DataHub.get_position(self.contract_id)
        
        # 3. 執行實體策略（如 SMA 交叉）的 on_bar 邏輯
        try:
            await self.on_bar(df)
        except Exception as e:
            logger.error(f"❌ 策略 {self.strategy_name} 執行異常: {str(e)}", exc_info=True)

    @abstractmethod
    async def on_bar(self, df: pd.DataFrame):
        """
        [抽象方法] 子類別必須實作此方法來定義具體的買賣信號
        """
        pass

    def get_pos_size(self) -> int:
        """
        快捷工具：獲取當前持倉數量（多頭為正，空頭為負，無倉為 0）
        """
        return self.current_position.size if self.current_position else 0