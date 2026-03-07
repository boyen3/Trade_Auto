# strategies/timer_test.py
import time
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide
from utils.logger import logger

class StressTestStrategy(BaseStrategy):
    def __init__(self, trading_service, market_service, notifier):
        super().__init__(trading_service, market_service)
        self.strategy_name = "Timer_Stress_Test"
        self.notifier = notifier
        self.last_trade_time = 0
        self.interval = 60  # 每 60 秒強制交易一次
        self.is_processing = False

    async def on_bar(self, df):
        """
        這個策略不依賴 K 線，而是依賴時間戳記
        """
        current_time = time.time()
        
        # 檢查冷卻時間
        if self.is_processing or (current_time - self.last_trade_time < self.interval):
            return

        self.is_processing = True
        
        try:
            # 獲取當前持倉 (由 DataHub 自動更新)
            pos_size = self.get_pos_size()
            
            if pos_size == 0:
                # 1. 無持倉時 -> 買入進場
                logger.info("🧪 [測試進場] 無持倉，發送市價買單...")
                await self._trade_and_notify("BUY", 2, 0) # 2=Market, 0=Side.BUY
            else:
                # 2. 有持倉時 -> 反手賣出 (平倉)
                logger.info(f"🧪 [測試平倉] 目前持倉 {pos_size}，發送市價賣單...")
                await self._trade_and_notify("SELL/CLOSE", 2, 1) # 2=Market, 1=Side.SELL

            self.last_trade_time = current_time
        except Exception as e:
            logger.error(f"❌ 測試策略異常: {e}")
        finally:
            self.is_processing = False

    async def _trade_and_notify(self, action, order_type, side):
        # 呼叫我們測試成功的 TradingService
        order_id = await self.ts.place_order(
            self.account_id, self.contract_id, order_type, side, 1
        )
        
        if order_id:
            # 發送 Telegram 通知
            await self.notifier.send_trade_notification(
                self.strategy_name, action, self.contract_id, "TEST_PRICE", 1
            )
            return order_id
        return None