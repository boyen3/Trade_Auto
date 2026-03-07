# strategies/rapid_test.py
import pandas as pd
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide
from utils.logger import logger

class RapidTestStrategy(BaseStrategy):
    def __init__(self, trading_service, market_service, notifier):
        super().__init__(trading_service, market_service)
        self.strategy_name = "Rapid_Stress_Test"
        self.notifier = notifier
        self.fast_period = 3  # 極短均線
        self.slow_period = 5
        self.is_processing = False

    async def on_bar(self, df: pd.DataFrame):
        if self.is_processing or len(df) < self.slow_period:
            return

        # 計算均線
        df['fast'] = df['c'].rolling(window=self.fast_period).mean()
        df['slow'] = df['c'].rolling(window=self.slow_period).mean()

        current_price = df['c'].iloc[-1]
        last_fast = df['fast'].iloc[-2]
        last_slow = df['slow'].iloc[-2]
        curr_fast = df['fast'].iloc[-1]
        curr_slow = df['slow'].iloc[-1]

        # 判定交叉
        gold_cross = last_fast <= last_slow and curr_fast > curr_slow
        death_cross = last_fast >= last_slow and curr_fast < curr_slow

        pos_size = self.get_pos_size()

        # 1. 黃金交叉 -> 買入 (若無持倉)
        if gold_cross and pos_size <= 0:
            await self.execute_trade("BUY", 2, OrderSide.BUY, current_price)

        # 2. 死亡交叉 -> 賣出 (若有持倉)
        elif death_cross and pos_size > 0:
            await self.execute_trade("SELL", 2, OrderSide.SELL, current_price)

    async def execute_trade(self, action, order_type, side, price):
        self.is_processing = True
        logger.info(f"🧪 壓力測試觸發: {action}")
        
        # 下單 (Type 2 = Market)
        order_id = await self.ts.place_order(
            self.account_id, self.contract_id, order_type, side, 1
        )
        
        if order_id:
            # 同步發送 Telegram
            await self.notifier.send_trade_notification(
                self.strategy_name, action, self.contract_id, price, 1
            )
        
        self.is_processing = False