# strategies/sma_cross.py
import pandas as pd
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from utils.logger import logger

class SmaCrossStrategy(BaseStrategy):
    def __init__(self, trading_service, market_service, fast_period=5, slow_period=20):
        """
        初始化 SMA 交叉策略
        :param fast_period: 短期均線週期 (預設 5)
        :param slow_period: 長期均線週期 (預設 20)
        """
        super().__init__(trading_service, market_service)
        self.fast_period = fast_period
        self.slow_period = slow_period

    async def on_bar(self, df: pd.DataFrame):
        """
        每當 K 線更新時由 EventEngine 觸發。
        在此實作黃金交叉與死亡交叉邏輯。
        """
        # 1. 載入持久化記憶 (用於檢查進場狀態或紀錄)
        memory = self.load_memory()
        
        # 2. 數據長度檢查，確保足以計算均線
        if len(df) < self.slow_period:
            return

        # 3. 計算技術指標
        df['sma_fast'] = df['c'].rolling(window=self.fast_period).mean()
        df['sma_slow'] = df['c'].rolling(window=self.slow_period).mean()

        # 取得最後兩根棒線的值，用於判斷「交叉」瞬間
        last_fast = df['sma_fast'].iloc[-1]
        last_slow = df['sma_slow'].iloc[-1]
        prev_fast = df['sma_fast'].iloc[-2]
        prev_slow = df['sma_slow'].iloc[-2]

        # 4. 獲取目前持倉快照 (由基底類別自動維護)
        current_size = self.get_pos_size()
        current_price = df['c'].iloc[-1]

        # --- 交易執行邏輯 ---

        # 黃金交叉: 快線由下往上穿越慢線
        if prev_fast <= prev_slow and last_fast > last_slow:
            # 如果目前沒有多單 (倉位 <= 0)
            if current_size <= 0:
                logger.info(f"🚀 [策略信號] SMA 黃金交叉！價格: {current_price}")
                
                # 執行買入指令
                order_id = await self.ts.place_order(
                    account_id=self.account_id,
                    contract_id=self.contract_id,
                    order_type=OrderType.MARKET,
                    side=OrderSide.BUY,
                    size=1
                )
                
                if order_id:
                    # 5. 紀錄到持久化檔案 (data/system_state.json)
                    self.save_memory(
                        last_action="BUY", 
                        entry_price=current_price,
                        last_signal_time=str(df.index[-1])
                    )

        # 死亡交叉: 快線由上往下穿越慢線
        elif prev_fast >= prev_slow and last_fast < last_slow:
            # 如果目前持有對沖多單或尚未平倉 (倉位 >= 0)
            if current_size >= 0:
                logger.info(f"🔻 [策略信號] SMA 死亡交叉！價格: {current_price}")
                
                # 執行賣出指令
                order_id = await self.ts.place_order(
                    account_id=self.account_id,
                    contract_id=self.contract_id,
                    order_type=OrderType.MARKET,
                    side=OrderSide.SELL,
                    size=1
                )
                
                if order_id:
                    # 紀錄到持久化檔案
                    self.save_memory(
                        last_action="SELL", 
                        exit_price=current_price,
                        last_signal_time=str(df.index[-1])
                    )

                    # 這裡加入發送通知
                    await self.notifier.send_trade_notification(
                        self.strategy_name, "BUY", self.contract_id, current_price, 1
                    )