# strategies/smc_strategy.py
"""
SMC 聰明錢策略（Smart Money Concept）
======================================
進場邏輯（需同時滿足）：
  1. 識別 Order Block（OB）：最後一根推動波之前的反向 K 棒
  2. 結構突破（BOS）：價格突破前高/前低，確認趨勢方向
  3. 回測 OB 區域：價格回踩到 OB 範圍內
  4. FVG 缺口確認：回測途中存在未填補的 FVG，作為精準進場點

出場邏輯：
  - 止損：ATR × atr_sl_mult 放在 OB 外側
  - 止盈：ATR × atr_tp_mult（預設 RR = 1:2）

使用方式（main.py）：
  from strategies.smc_strategy import SMCStrategy
  strategy = SMCStrategy(
      trading_service=self.trading_service,
      market_service=self.market_service,
      notifier=self.notifier
  )
  strategy.account_id = self.config["account_id"]
  strategy.contract_id = self.config["symbol"]
  self.event_engine.register("ON_BAR", strategy.on_bar)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from core.data_hub import DataHub
from utils.logger import logger


@dataclass
class OrderBlock:
    """Order Block 資料結構"""
    high: float
    low: float
    side: str          # "bullish" | "bearish"
    bar_index: int
    valid: bool = True


@dataclass
class FVG:
    """Fair Value Gap 資料結構"""
    high: float
    low: float
    side: str          # "bullish" | "bearish"
    bar_index: int
    filled: bool = False


class SMCStrategy(BaseStrategy):

    def __init__(self, trading_service, market_service, notifier,
                 ob_lookback: int = 20,
                 atr_period: int = 14,
                 atr_sl_mult: float = 1.5,
                 atr_tp_mult: float = 3.0,
                 size: int = 1):
        """
        5 分鐘 K 棒建議參數：
          ob_lookback=20  → 覆蓋約 100 分鐘結構
          atr_period=14   → 標準 ATR
          atr_sl_mult=1.5 → 止損約 1.5 個 ATR
          atr_tp_mult=3.0 → 止盈約 3 個 ATR（RR ≈ 1:2）
        """
        super().__init__(trading_service, market_service)
        self.strategy_name = "SMC_OB_FVG"
        self.notifier = notifier
        self.ob_lookback  = ob_lookback
        self.atr_period   = atr_period
        self.atr_sl_mult  = atr_sl_mult
        self.atr_tp_mult  = atr_tp_mult
        self.size         = size

        self._is_processing = False
        self._last_signal_bar = -1   # 避免同一根 K 重複下單

        logger.info(
            f"[SMC] 策略初始化 | OB回望:{ob_lookback} | "
            f"ATR週期:{atr_period} | SL倍數:{atr_sl_mult} | TP倍數:{atr_tp_mult}"
        )

    # ──────────────────────────────────────────
    # 主邏輯入口
    # ──────────────────────────────────────────
    async def on_bar(self, df: pd.DataFrame):
        min_bars = self.ob_lookback + self.atr_period + 5
        if self._is_processing or len(df) < min_bars:
            return

        # 已有持倉不進新單
        if self.get_pos_size() != 0:
            return

        # 避免同一根 K 棒重複觸發
        current_bar_idx = len(df) - 1
        if current_bar_idx == self._last_signal_bar:
            return

        try:
            self._is_processing = True
            await self._evaluate(df, current_bar_idx)
        except Exception as e:
            logger.error(f"[SMC] on_bar 異常: {e}")
        finally:
            self._is_processing = False

    async def _evaluate(self, df: pd.DataFrame, bar_idx: int):
        # 1. 計算 ATR
        atr = self._calc_atr(df)
        if atr <= 0:
            return

        # 2. 識別 Order Block
        bull_ob = self._find_order_block(df, "bullish")
        bear_ob = self._find_order_block(df, "bearish")

        # 3. 識別 FVG
        bull_fvg = self._find_fvg(df, "bullish")
        bear_fvg = self._find_fvg(df, "bearish")

        # 4. 結構判斷（BOS）
        bos_bull = self._check_bos(df, "bullish")
        bos_bear = self._check_bos(df, "bearish")

        current_price = df['c'].iloc[-1]

        # ── 做多條件 ──────────────────────────
        # BOS 向上 + 價格回測到多頭 OB 內 + 存在多頭 FVG
        if bos_bull and bull_ob and bull_ob.valid:
            in_ob = bull_ob.low <= current_price <= bull_ob.high
            fvg_confirm = bull_fvg and not bull_fvg.filled and bull_fvg.low <= current_price <= bull_fvg.high

            if in_ob and fvg_confirm:
                sl_ticks = self._atr_to_ticks(atr * self.atr_sl_mult)
                tp_ticks = self._atr_to_ticks(atr * self.atr_tp_mult)
                logger.info(
                    f"[SMC] 🟢 多頭訊號 | 價格:{current_price:.2f} | "
                    f"OB:[{bull_ob.low:.2f}-{bull_ob.high:.2f}] | "
                    f"FVG:[{bull_fvg.low:.2f}-{bull_fvg.high:.2f}] | "
                    f"ATR:{atr:.2f} SL:{sl_ticks}t TP:{tp_ticks}t"
                )
                await self._place_order(OrderSide.BUY, current_price, sl_ticks, tp_ticks, bar_idx)

        # ── 做空條件 ──────────────────────────
        # BOS 向下 + 價格回測到空頭 OB 內 + 存在空頭 FVG
        elif bos_bear and bear_ob and bear_ob.valid:
            in_ob = bear_ob.low <= current_price <= bear_ob.high
            fvg_confirm = bear_fvg and not bear_fvg.filled and bear_fvg.low <= current_price <= bear_fvg.high

            if in_ob and fvg_confirm:
                sl_ticks = self._atr_to_ticks(atr * self.atr_sl_mult)
                tp_ticks = self._atr_to_ticks(atr * self.atr_tp_mult)
                logger.info(
                    f"[SMC] 🔴 空頭訊號 | 價格:{current_price:.2f} | "
                    f"OB:[{bear_ob.low:.2f}-{bear_ob.high:.2f}] | "
                    f"FVG:[{bear_fvg.low:.2f}-{bear_fvg.high:.2f}] | "
                    f"ATR:{atr:.2f} SL:{sl_ticks}t TP:{tp_ticks}t"
                )
                await self._place_order(OrderSide.SELL, current_price, sl_ticks, tp_ticks, bar_idx)

    # ──────────────────────────────────────────
    # Order Block 識別
    # ──────────────────────────────────────────
    def _find_order_block(self, df: pd.DataFrame, side: str) -> Optional[OrderBlock]:
        """
        多頭 OB：最後一根下跌 K 棒，之後緊接著出現強勢上漲突破前高
        空頭 OB：最後一根上漲 K 棒，之後緊接著出現強勢下跌突破前低
        取最近 ob_lookback 根 K 棒內最新的有效 OB
        """
        window = df.iloc[-(self.ob_lookback + 5):-1]   # 不含最新一根
        opens  = window['o'].values
        closes = window['c'].values
        highs  = window['h'].values
        lows   = window['l'].values

        result = None

        for i in range(len(window) - 2):
            if side == "bullish":
                # 下跌 K（close < open）
                is_down_candle = closes[i] < opens[i]
                # 下一根強勢上漲（close > 當前 K 的 high）
                next_strong_up = closes[i + 1] > highs[i]
                if is_down_candle and next_strong_up:
                    result = OrderBlock(
                        high=highs[i],
                        low=lows[i],
                        side="bullish",
                        bar_index=i
                    )
            else:  # bearish
                # 上漲 K（close > open）
                is_up_candle = closes[i] > opens[i]
                # 下一根強勢下跌（close < 當前 K 的 low）
                next_strong_down = closes[i + 1] < lows[i]
                if is_up_candle and next_strong_down:
                    result = OrderBlock(
                        high=highs[i],
                        low=lows[i],
                        side="bearish",
                        bar_index=i
                    )

        return result

    # ──────────────────────────────────────────
    # FVG 識別
    # ──────────────────────────────────────────
    def _find_fvg(self, df: pd.DataFrame, side: str) -> Optional[FVG]:
        """
        多頭 FVG：K[i] 的 high < K[i+2] 的 low（中間 K 棒留下缺口）
        空頭 FVG：K[i] 的 low  > K[i+2] 的 high
        取最近 ob_lookback 根內最新的未填補 FVG
        """
        window = df.iloc[-(self.ob_lookback + 5):-1]
        highs = window['h'].values
        lows  = window['l'].values

        result = None

        for i in range(len(window) - 2):
            if side == "bullish":
                gap_exists = highs[i] < lows[i + 2]
                if gap_exists:
                    fvg = FVG(
                        high=lows[i + 2],
                        low=highs[i],
                        side="bullish",
                        bar_index=i
                    )
                    # 檢查缺口是否已被填補（後續有 K 棒的 low 進入缺口）
                    if i + 3 < len(window):
                        subsequent_lows = lows[i + 3:]
                        if any(l <= fvg.high for l in subsequent_lows):
                            fvg.filled = True
                    result = fvg
            else:  # bearish
                gap_exists = lows[i] > highs[i + 2]
                if gap_exists:
                    fvg = FVG(
                        high=lows[i],
                        low=highs[i + 2],
                        side="bearish",
                        bar_index=i
                    )
                    if i + 3 < len(window):
                        subsequent_highs = highs[i + 3:]
                        if any(h >= fvg.low for h in subsequent_highs):
                            fvg.filled = True
                    result = fvg

        return result

    # ──────────────────────────────────────────
    # BOS 結構突破確認
    # ──────────────────────────────────────────
    def _check_bos(self, df: pd.DataFrame, side: str) -> bool:
        """
        多頭 BOS：最新收盤價突破過去 ob_lookback 根的最高點
        空頭 BOS：最新收盤價突破過去 ob_lookback 根的最低點
        """
        window = df.iloc[-(self.ob_lookback + 1):-1]
        current_close = df['c'].iloc[-1]

        if side == "bullish":
            swing_high = window['h'].max()
            return current_close > swing_high
        else:
            swing_low = window['l'].min()
            return current_close < swing_low

    # ──────────────────────────────────────────
    # ATR 計算
    # ──────────────────────────────────────────
    def _calc_atr(self, df: pd.DataFrame) -> float:
        if len(df) < self.atr_period + 1:
            return 0.0
        window = df.tail(self.atr_period + 1).copy()
        window['prev_c'] = window['c'].shift(1)
        window['tr'] = window.apply(
            lambda r: max(
                r['h'] - r['l'],
                abs(r['h'] - r['prev_c']) if pd.notna(r['prev_c']) else 0,
                abs(r['l'] - r['prev_c']) if pd.notna(r['prev_c']) else 0
            ), axis=1
        )
        return window['tr'].tail(self.atr_period).mean()

    def _atr_to_ticks(self, atr_value: float, tick_size: float = 0.25) -> int:
        """ATR 點數轉換為 ticks（MNQ 最小跳動 0.25 點）"""
        ticks = int(round(atr_value / tick_size))
        return max(ticks, 4)   # 最少 4 ticks，避免過小

    # ──────────────────────────────────────────
    # 下單
    # ──────────────────────────────────────────
    async def _place_order(self, side: OrderSide, price: float,
                           sl_ticks: int, tp_ticks: int, bar_idx: int):
        # 風控確認
        engine_paused = False
        try:
            import __main__
            engine_paused = getattr(__main__.ProjectXEngine, 'strategy_paused', False)
        except Exception:
            pass

        if engine_paused:
            logger.info("[SMC] 策略已暫停，跳過下單")
            return

        order_id = await self.ts.place_order(
            self.account_id,
            self.contract_id,
            OrderType.MARKET,
            side,
            self.size,
            sl_ticks=sl_ticks,
            tp_ticks=tp_ticks
        )

        if order_id:
            self._last_signal_bar = bar_idx
            direction = "BUY" if side == OrderSide.BUY else "SELL"
            logger.info(
                f"[SMC] ✅ 下單成功 #{order_id} | {direction} {self.size}口 "
                f"@ {price:.2f} | SL:{sl_ticks}t TP:{tp_ticks}t"
            )
            if self.notifier:
                await self.notifier.send_trade_notification(
                    self.strategy_name, direction,
                    self.contract_id, price, self.size
                )
        else:
            logger.error(f"[SMC] ❌ 下單失敗")
