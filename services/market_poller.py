# services/market_poller.py
import asyncio
from datetime import datetime
from typing import List, Dict
from services.market_data import MarketDataService
from core.data_hub import DataHub
from core.constants import BarUnit
from utils.logger import logger

class MarketPoller:
    def __init__(self, market_service: MarketDataService):
        self.ms = market_service
        self._is_running = False
        self._symbols: List[str] = []
        self._polling_tasks: Dict[str, asyncio.Task] = {}

    async def start(self, symbols: List[str], interval: float = 30.0):
        """
        啟動市場輪詢器
        :param symbols: 要監控的合約 ID 列表 (例如 ["CME.NQ.H24"])
        :param interval: 輪詢間隔，預設 30 秒更新一次 K 線
        """
        self._is_running = True
        self._symbols = symbols
        logger.info(f"📊 市場輪詢器啟動 - 監控商品: {symbols} (間隔: {interval}s)")

        # 為每個商品建立獨立的輪詢任務，避免單一商品報錯影響全部
        for symbol in self._symbols:
            task = asyncio.create_task(self._poll_symbol(symbol, interval))
            self._polling_tasks[symbol] = task

    async def _poll_symbol(self, symbol: str, interval: float):
        """單一商品的輪詢邏輯"""
        while self._is_running:
            try:
                # 1. 向 API 請求最新 K 線
                # includePartialBar=True 確保能拿到尚未收盤的那根 K 線 (模擬即時價)
                df = await self.ms.get_bars_df(symbol, unit=BarUnit.MINUTE, limit=100)
                
                if not df.empty:
                    # 2. 更新至 DataHub
                    DataHub.update_bars(symbol, df)
                    
                    # 3. 提取最後一根 K 線的收盤價作為「當前市價」
                    # 這對於 PositionModel 計算未實現損益至關重要
                    last_price = df['c'].iloc[-1]
                    
                    # 4. 同步更新 DataHub 中該商品的持倉現價
                    pos = DataHub.get_position(symbol)
                    if pos:
                        pos.currentPrice = last_price
                    
                    # logger.debug(f"📈 {symbol} 數據已更新, 最新價: {last_price}")

            except Exception as e:
                logger.error(f"❌ MarketPoller 輪詢 {symbol} 異常: {str(e)}")
            
            # 配合 Win7 網路穩定性，等待下一輪
            await asyncio.sleep(interval)

    def stop(self):
        self._is_running = False
        for task in self._polling_tasks.values():
            task.cancel()
        logger.info("📊 市場輪詢器已停止")