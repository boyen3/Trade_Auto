# strategies/tl_strategy.py
"""
Trendline Pullback Strategy — 趨勢線回踩策略
=============================================
邏輯：
  1. 在 15M 上用擺動高低點識別有效趨勢線（≥3點，R²≥0.92）
  2. 等 5M 收盤價回踩趨勢線（距離 ≤ 0.5 ATR）
  3. 方向需與 1H EMA50 一致
  4. 進場，止損趨勢線對面 0.5 ATR，1.5R 部分止盈

加速設計：
  - 趨勢線每 N 根 15M bar 重算一次（不是每根 5M 都算）
  - ATR / EMA 快取
  - 早退設計，無效 bar 幾乎零成本
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime

from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from core.data_hub import DataHub
from utils.logger import logger


# ──────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────

@dataclass
class TrendLine:
    direction:   str    # "up" | "down"
    slope:       float  # 每根 15M bar 的斜率（點數）
    intercept:   float  # 在 anchor_bar_idx 的截距
    anchor_idx:  int    # 建立時的全域 bar_count（5M）
    anchor_price:float  # 建立時趨勢線的價格
    r2:          float  # 擬合品質
    touch_count: int    # 觸及次數
    last_touch:  int    # 最後觸及的 bar_count
    cooldown_until: int = 0  # 冷卻到這個 bar_count 才能再進場

    def price_at(self, bar_count: int, bars_per_15m: int = 3) -> float:
        """計算在目前 bar_count 時，趨勢線的價格"""
        delta_15m = (bar_count - self.anchor_idx) / bars_per_15m
        return self.anchor_price + self.slope * delta_15m


@dataclass
class ActiveTrade:
    side:                str
    entry_price:         float
    sl_price:            float
    tp_price:            float
    tl_direction:        str    # 對應的趨勢線方向
    partial_closed:      bool  = False
    trailing_active:     bool  = False
    trailing_sl:         float = 0.0
    trailing_last_moved: float = 0.0
    is_closed:           bool  = False
    order_id:            Optional[int] = None


# ──────────────────────────────────────────
# 主策略
# ──────────────────────────────────────────

class TLStrategy(BaseStrategy):

    BACKTEST_PARAMS = {
        "htf_ema":            50,
        "htf_timeframe":      "1h",
        "swing_lookback":     3,       # 擺動點前後各幾根
        "tl_scan_bars":       60,      # 掃多少根 15M bar
        "tl_min_points":      3,       # 最少幾個擺動點
        "tl_r2_min":          0.92,   # 已棄用，保留向下相容
        "tl_confirm_atr":     0.5,    # 確認點誤差門檻（ATR 倍數）    # R² 門檻
        "tl_slope_min":       0.05,    # 最小斜率（ATR 倍數，排除水平線）
        "tl_max_lines":       3,       # 最多保留幾條趨勢線
        "tl_refresh_bars":    3,       # 每幾根 5M bar 重算趨勢線
        "entry_atr_mult":     0.5,     # 進場距離門檻（ATR 倍數）
        "sl_atr_mult":        0.5,     # 止損緩衝
        "min_rr":             1.5,
        "sl_max_atr_mult":    1.5,
        "partial_tp_r":       1.5,
        "partial_close_ratio":0.5,
        "trailing_atr_mult":  0.5,
        "trailing_r":         1.5,
        "atr_period":         14,
        "cooldown_bars":      3,       # 同一條線進場後冷卻根數
        "max_daily_trades":   5,
        "daily_loss_limit_r": 2.0,
        "session_london_start":   7,
        "session_london_end":     12,
        "session_ny_start":       13,
        "session_ny_start_min":   30,
        "session_ny_end":         20,
    }

    def __init__(self, trading_service, market_service, notifier, engine,
                 htf_ema: int = 50,
                 htf_timeframe: str = "1h",
                 swing_lookback: int = 3,
                 tl_scan_bars: int = 60,
                 tl_min_points: int = 3,
                 tl_r2_min: float = 0.92,
                 tl_confirm_atr: float = 0.5,
                 tl_slope_min: float = 0.05,
                 tl_max_lines: int = 3,
                 tl_refresh_bars: int = 3,
                 entry_atr_mult: float = 0.5,
                 sl_atr_mult: float = 0.5,
                 min_rr: float = 1.5,
                 sl_max_atr_mult: float = 1.5,
                 partial_tp_r: float = 1.5,
                 partial_close_ratio: float = 0.5,
                 trailing_atr_mult: float = 0.5,
                 trailing_r: float = 1.5,
                 atr_period: int = 14,
                 cooldown_bars: int = 3,
                 max_daily_trades: int = 5,
                 daily_loss_limit_r: float = 2.0,
                 session_london_start: int = 7,
                 session_london_end: int = 12,
                 session_ny_start: int = 13,
                 session_ny_start_min: int = 30,
                 session_ny_end: int = 20,
                 contract_id: str = "CON.F.US.MNQ.H26",
                 **kwargs):

        super().__init__(trading_service, market_service)
        self.notifier    = notifier
        self.engine      = engine
        self.contract_id = contract_id

        self.htf_ema            = htf_ema
        self.htf_timeframe      = htf_timeframe
        self.swing_lookback     = swing_lookback
        self.tl_scan_bars       = tl_scan_bars
        self.tl_min_points      = tl_min_points
        self.tl_r2_min          = tl_r2_min
        self.tl_confirm_atr     = tl_confirm_atr
        self.tl_slope_min       = tl_slope_min
        self.tl_max_lines       = tl_max_lines
        self.tl_refresh_bars    = tl_refresh_bars
        self.entry_atr_mult     = entry_atr_mult
        self.sl_atr_mult        = sl_atr_mult
        self.min_rr             = min_rr
        self.sl_max_atr_mult    = sl_max_atr_mult
        self.partial_tp_r       = partial_tp_r
        self.partial_close_ratio = partial_close_ratio
        self.trailing_atr_mult  = trailing_atr_mult
        self.trailing_r         = trailing_r
        self.atr_period         = atr_period
        self.cooldown_bars      = cooldown_bars
        self.max_daily_trades   = max_daily_trades
        self.daily_loss_limit_r = daily_loss_limit_r
        self.session_london_start  = session_london_start
        self.session_london_end    = session_london_end
        self.session_ny_start      = session_ny_start
        self.session_ny_start_min  = session_ny_start_min
        self.session_ny_end        = session_ny_end

        self.TICK            = 0.25
        self.TICKS_PER_POINT = 4
        self.account_id: int = 0

        # 狀態
        self.active_trade:  Optional[ActiveTrade] = None
        self._trendlines:   List[TrendLine] = []
        self._bar_count:    int = 0
        self._last_tl_refresh: int = -999

        # 日內風控
        self._current_day:  Optional[object] = None
        self._daily_trades: int = 0
        self._daily_pnl_r:  float = 0.0

        # ATR 快取
        self._cached_atr: float = 10.0
        self._atr_bar:    int   = -1

        # HTF 預算（回測引擎注入）
        self._htf_precomputed: Optional[dict] = None

        # Skip 計數
        self.skip_counts: dict = {
            "session": 0, "htf_neutral": 0, "no_tl": 0,
            "no_signal": 0, "sl_too_large": 0, "rr_too_low": 0,
            "daily_limit": 0, "cooldown": 0,
        }

        self.dashboard: dict = {
            "session_open": False,
            "status": "初始化中...",
            "active_trade": None,
            "trendlines": 0,
        }

    # ──────────────────────────────────────────────────────────────────
    # 主流程
    # ──────────────────────────────────────────────────────────────────

    async def on_bar(self, symbol: str, df) -> None:
        if symbol != self.contract_id or len(df) < 50:
            return

        self._bar_count += 1

        c_arr = df['c'].values
        o_arr = df['o'].values
        h_arr = df['h'].values
        l_arr = df['l'].values

        c_last = float(c_arr[-1])
        h_last = float(h_arr[-1])
        l_last = float(l_arr[-1])

        # ── 1. 時段過濾 ───────────────────────────────────────────────
        bt = df.index[-1]
        if not self._is_session_open(bt):
            self.skip_counts["session"] += 1
            return

        # ── 2. 日內重置 ───────────────────────────────────────────────
        self._reset_daily(bt)

        # ── 3. 日內風控 ───────────────────────────────────────────────
        if self._daily_trades >= self.max_daily_trades or \
           self._daily_pnl_r  <= -self.daily_loss_limit_r:
            self.skip_counts["daily_limit"] += 1
            return

        # ── 4. 管理持倉 ───────────────────────────────────────────────
        if self.active_trade:
            self._manage_trade(c_last, h_last, l_last)
            return

        # ── 5. ATR 快取 ───────────────────────────────────────────────
        if self._atr_bar != self._bar_count:
            self._cached_atr = self._calc_atr(h_arr, l_arr, c_arr)
            self._atr_bar    = self._bar_count
        atr = self._cached_atr
        if atr <= 0:
            return

        # ── 6. HTF 趨勢 ───────────────────────────────────────────────
        trend = self._get_htf_trend(df, c_arr)
        if trend == "neutral":
            self.skip_counts["htf_neutral"] += 1
            return

        # ── 7. 更新趨勢線（每 tl_refresh_bars 根才重算）──────────────
        if self._bar_count - self._last_tl_refresh >= self.tl_refresh_bars:
            self._update_trendlines(h_arr, l_arr, c_arr, atr)
            self._last_tl_refresh = self._bar_count

        if not self._trendlines:
            self.skip_counts["no_tl"] += 1
            return

        # ── 8. 找進場訊號 ─────────────────────────────────────────────
        signal = self._find_entry(trend, c_last, h_last, l_last, atr)
        if signal is None:
            return

        # ── 9. 下單 ───────────────────────────────────────────────────
        await self._place(signal)

    # ──────────────────────────────────────────────────────────────────
    # 時段
    # ──────────────────────────────────────────────────────────────────

    def _is_session_open(self, bt) -> bool:
        try:
            h = bt.hour; m = bt.minute
            if self.session_london_start <= h < self.session_london_end:
                return True
            ny0 = self.session_ny_start * 60 + self.session_ny_start_min
            if ny0 <= h * 60 + m < self.session_ny_end * 60:
                return True
        except Exception:
            return True
        return False

    # ──────────────────────────────────────────────────────────────────
    # 日內重置
    # ──────────────────────────────────────────────────────────────────

    def _reset_daily(self, bt) -> None:
        try:
            today = bt.date()
            if today != self._current_day:
                self._current_day  = today
                self._daily_trades = 0
                self._daily_pnl_r  = 0.0
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # ATR
    # ──────────────────────────────────────────────────────────────────

    def _calc_atr(self, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> float:
        # 快取：同一根 bar 只算一次
        if self._atr_bar == self._bar_count:
            return self._cached_atr
        p = self.atr_period
        if len(h) < p + 1:
            v = float(np.mean(h - l)) if len(h) > 0 else 1.0
        else:
            h2 = h[-(p+1):-1]; h3 = h[-p:]
            l2 = l[-(p+1):-1]; l3 = l[-p:]
            c2 = c[-(p+1):-1]
            tr = np.maximum(h3-l3, np.maximum(np.abs(h3-c2), np.abs(l3-c2)))
            v  = float(tr.mean())
        self._cached_atr = v
        self._atr_bar    = self._bar_count
        return v

    # ──────────────────────────────────────────────────────────────────
    # HTF 趨勢
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _ema(closes: np.ndarray, period: int) -> np.ndarray:
        k = 2.0 / (period + 1)
        e = np.empty(len(closes))
        e[0] = closes[0]
        for i in range(1, len(closes)):
            e[i] = closes[i] * k + e[i-1] * (1 - k)
        return e

    def _get_htf_trend(self, df, c_arr: np.ndarray) -> str:
        pc = self._htf_precomputed
        if pc:
            key   = f"{self.htf_timeframe}_{self.htf_ema}"
            entry = pc.get(key)
            if entry:
                st, sd    = entry
                bs        = getattr(self, '_backtest_bar_store', None)
                cutoff_ns = getattr(bs, 'current_bar_time_ns', None) if bs else None
                if cutoff_ns is not None:
                    idx = int(np.searchsorted(st, cutoff_ns, side='right')) - 1
                    if 0 <= idx < len(sd):
                        return sd[idx]

        try:
            bs = getattr(self.engine, 'bar_store', None)
            if bs:
                hdf = bs.load(self.contract_id, self.htf_timeframe,
                              limit=self.htf_ema + 10)
                if hdf is not None and len(hdf) >= self.htf_ema:
                    cl = hdf['c'].values
                    e  = self._ema(cl, self.htf_ema)
                    c, ev = cl[-1], e[-1]
                    if c > ev * 1.001: return "bull"
                    if c < ev * 0.999: return "bear"
                    return "neutral"
        except Exception:
            pass

        if len(c_arr) >= self.htf_ema:
            e = self._ema(c_arr, self.htf_ema)
            c, ev = c_arr[-1], e[-1]
            if c > ev * 1.001: return "bull"
            if c < ev * 0.999: return "bear"
        return "neutral"

    # ──────────────────────────────────────────────────────────────────
    # 趨勢線識別
    # ──────────────────────────────────────────────────────────────────

    def _find_swing_points(self, h: np.ndarray, l: np.ndarray,
                           n: int) -> tuple:
        """
        向量化擺動點識別（相容 Python 3.8 / numpy < 1.20）。
        用 as_strided 手動建立 sliding window，避免 Python for-loop。
        """
        lb = self.swing_lookback
        w  = 2 * lb + 1
        empty = np.array([], dtype=np.int64)

        if n < w:
            return (empty, np.array([])), (empty, np.array([]))

        # 用 as_strided 建立 shape=(n-w+1, w) 的 view
        from numpy.lib.stride_tricks import as_strided
        h_n  = h[:n]; l_n = l[:n]
        s    = h_n.strides[0]
        rows = n - w + 1

        win_h = as_strided(h_n, shape=(rows, w), strides=(s, s))
        win_l = as_strided(l_n, shape=(rows, w), strides=(s, s))

        # max/min over axis=1（一次向量化，無迴圈）
        roll_max = win_h.max(axis=1)   # shape: (rows,)
        roll_min = win_l.min(axis=1)

        # 中心點 index = lb .. n-lb-1，對應 roll_max/min 的 index 0..rows-1
        center_h = h_n[lb: lb + rows]
        center_l = l_n[lb: lb + rows]

        base   = np.arange(lb, lb + rows, dtype=np.int64)
        sh_idx = base[center_h == roll_max]
        sh_val = center_h[center_h == roll_max]
        sl_idx = base[center_l == roll_min]
        sl_val = center_l[center_l == roll_min]

        return (sh_idx, sh_val), (sl_idx, sl_val)

    def _two_point_trendline(self, i1: int, p1: float, i2: int, p2: float,
                              all_idx: np.ndarray, all_val: np.ndarray,
                              atr: float, direction: str) -> Optional[TrendLine]:
        """
        用兩個擺動點連線，用其餘點做確認（誤差 < tl_confirm_atr_mult * ATR）。
        至少需要 1 個確認點（共 3 點）。
        """
        if i2 == i1:
            return None
        slope     = (p2 - p1) / (i2 - i1)
        intercept = p1 - slope * i1

        # 斜率門檻（排除水平線）
        if abs(slope) < self.tl_slope_min * atr / 3:
            return None

        # 用其餘擺動點確認（不含 i1, i2 本身）
        confirm_thresh = self.tl_confirm_atr * atr
        confirms = 0
        for idx, val in zip(all_idx, all_val):
            if idx == i1 or idx == i2:
                continue
            expected = slope * idx + intercept
            if abs(val - expected) <= confirm_thresh:
                confirms += 1

        if confirms < 1:  # 至少需要 1 個確認點
            return None

        # 趨勢線在陣列最後一根的價格
        last_n     = int(all_idx[-1]) if len(all_idx) > 0 else i2
        last_price = slope * last_n + intercept

        return TrendLine(
            direction    = direction,
            slope        = slope,
            intercept    = intercept,
            anchor_idx   = self._bar_count,
            anchor_price = last_price,
            r2           = float(confirms + 2),  # 用確認點數代替 R²，方便排序
            touch_count  = confirms + 2,
            last_touch   = self._bar_count,
        )

    def _update_trendlines(self, h: np.ndarray, l: np.ndarray,
                            c: np.ndarray, atr: float) -> None:
        """
        重建趨勢線列表。
        新邏輯：任意兩個擺動點連線，用第三點確認，不依賴 R²。
        這樣更接近人眼畫線的方式，訊號數大幅增加。
        """
        scan_5m = self.tl_scan_bars * 3
        n       = min(len(h), scan_5m)
        min_pts = self.swing_lookback * 2 + 1
        if n < min_pts * 3:
            return

        hs = h[-n:]; ls = l[-n:]

        # 找擺動點（向量化）
        (sh_idx, sh_val), (sl_idx, sl_val) = self._find_swing_points(hs, ls, n)

        # 去除間距太近的擺動點（< swing_lookback 根）
        def dedupe(idx, val):
            if len(idx) == 0:
                return idx, val
            keep = [0]
            for i in range(1, len(idx)):
                if idx[i] - idx[keep[-1]] >= self.swing_lookback:
                    keep.append(i)
            return idx[keep], val[keep]

        sh_idx, sh_val = dedupe(sh_idx, sh_val)
        sl_idx, sl_val = dedupe(sl_idx, sl_val)

        new_lines = []

        # 下降趨勢線：任意兩個相鄰擺動高點連線
        if len(sh_idx) >= 3:
            for i in range(len(sh_idx) - 1):
                tl = self._two_point_trendline(
                    int(sh_idx[i]), sh_val[i],
                    int(sh_idx[i+1]), sh_val[i+1],
                    sh_idx, sh_val, atr, "down"
                )
                if tl and tl.slope < 0:
                    new_lines.append(tl)

        # 上升趨勢線：任意兩個相鄰擺動低點連線
        if len(sl_idx) >= 3:
            for i in range(len(sl_idx) - 1):
                tl = self._two_point_trendline(
                    int(sl_idx[i]), sl_val[i],
                    int(sl_idx[i+1]), sl_val[i+1],
                    sl_idx, sl_val, atr, "up"
                )
                if tl and tl.slope > 0:
                    new_lines.append(tl)

        # 保留觸及次數最多的幾條（更多觸及 = 更有效）
        new_lines.sort(key=lambda x: x.touch_count, reverse=True)
        self._trendlines = new_lines[:self.tl_max_lines]
        self.dashboard["trendlines"] = len(self._trendlines)

    # ──────────────────────────────────────────────────────────────────
    # 進場訊號
    # ──────────────────────────────────────────────────────────────────

    def _find_entry(self, trend: str, c: float, h: float, l: float,
                    atr: float) -> Optional[dict]:

        for tl in self._trendlines:
            # 冷卻中
            if self._bar_count < tl.cooldown_until:
                self.skip_counts["cooldown"] += 1
                continue

            tl_price = tl.price_at(self._bar_count)
            dist     = abs(c - tl_price)

            # 距離太遠
            if dist > self.entry_atr_mult * atr:
                continue

            # 上升趨勢線 → 做多（需 HTF 也是多頭）
            if tl.direction == "up" and trend == "bull":
                if c >= tl_price:  # 收盤在趨勢線上方（回踩但未跌破）
                    sl  = tl_price - self.sl_atr_mult * atr
                    sd  = c - sl
                    if sd <= 0: continue
                    if sd > self.sl_max_atr_mult * atr:
                        self.skip_counts["sl_too_large"] += 1
                        continue
                    tp = c + sd * self.min_rr
                    if (tp - c) / sd < self.min_rr:
                        self.skip_counts["rr_too_low"] += 1
                        continue
                    tl.cooldown_until = self._bar_count + self.cooldown_bars
                    return {"side": "long", "entry": c,
                            "sl": sl, "tp": tp, "atr": atr,
                            "tl_dir": tl.direction, "r2": tl.r2}

            # 下降趨勢線 → 做空（需 HTF 也是空頭）
            elif tl.direction == "down" and trend == "bear":
                if c <= tl_price:  # 收盤在趨勢線下方（回踩但未突破）
                    sl  = tl_price + self.sl_atr_mult * atr
                    sd  = sl - c
                    if sd <= 0: continue
                    if sd > self.sl_max_atr_mult * atr:
                        self.skip_counts["sl_too_large"] += 1
                        continue
                    tp = c - sd * self.min_rr
                    if (c - tp) / sd < self.min_rr:
                        self.skip_counts["rr_too_low"] += 1
                        continue
                    tl.cooldown_until = self._bar_count + self.cooldown_bars
                    return {"side": "short", "entry": c,
                            "sl": sl, "tp": tp, "atr": atr,
                            "tl_dir": tl.direction, "r2": tl.r2}

        self.skip_counts["no_tl"] += 1
        return None

    # ──────────────────────────────────────────────────────────────────
    # 持倉管理
    # ──────────────────────────────────────────────────────────────────

    def _manage_trade(self, c: float, h: float, l: float) -> None:
        t = self.active_trade
        if t is None or t.is_closed:
            self.active_trade = None
            return

        if DataHub.positions.get(self.contract_id) is None:
            self.active_trade = None
            return

        atr = self._cached_atr
        sd  = abs(t.entry_price - t.sl_price)
        if sd <= 0 or atr <= 0:
            return

        # 部分止盈
        if not t.partial_closed:
            hit = (t.side == "long"  and c >= t.entry_price + sd * self.partial_tp_r) or \
                  (t.side == "short" and c <= t.entry_price - sd * self.partial_tp_r)
            if hit:
                t.partial_closed = True
                t.sl_price       = t.entry_price
                is_bt = getattr(self.trading_service, '_is_backtest', False)
                if not is_bt:
                    import asyncio
                    asyncio.get_event_loop().create_task(
                        self.trading_service.partial_close_position(
                            self.account_id, self.contract_id,
                            self.partial_close_ratio))

        # 啟動移動止損
        if t.partial_closed and not t.trailing_active:
            hit = (t.side == "long"  and c >= t.entry_price + sd * self.trailing_r) or \
                  (t.side == "short" and c <= t.entry_price - sd * self.trailing_r)
            if hit:
                t.trailing_active = True
                t.trailing_sl     = t.entry_price

        # 更新移動止損
        if t.trailing_active:
            if t.side == "long":
                nsl = c - atr * self.trailing_atr_mult
                if nsl > t.trailing_sl and nsl > t.sl_price:
                    t.trailing_sl = nsl
                    self._move_sl(t, nsl)
            else:
                nsl = c + atr * self.trailing_atr_mult
                if nsl < t.trailing_sl and nsl < t.sl_price:
                    t.trailing_sl = nsl
                    self._move_sl(t, nsl)

    def _move_sl(self, t: ActiveTrade, nsl: float) -> None:
        if abs(nsl - t.trailing_last_moved) < self.TICK * 2:
            return
        t.sl_price            = nsl
        t.trailing_last_moved = nsl
        is_bt = getattr(self.trading_service, '_is_backtest', False)
        if not is_bt and t.order_id:
            import asyncio
            asyncio.get_event_loop().create_task(
                self.trading_service.modify_order(
                    self.account_id, t.order_id, stop_price=nsl))

    # ──────────────────────────────────────────────────────────────────
    # 下單
    # ──────────────────────────────────────────────────────────────────

    async def _place(self, sig: dict) -> None:
        side  = sig["side"]
        entry = sig["entry"]
        sl    = sig["sl"]
        tp    = sig["tp"]
        atr   = sig["atr"]
        sd    = abs(entry - sl)
        td    = abs(tp - entry)

        try:
            oid = await self.trading_service.place_order(
                account_id  = self.account_id,
                contract_id = self.contract_id,
                order_type  = OrderType.MARKET,
                side        = OrderSide.BUY if side == "long" else OrderSide.SELL,
                size        = 1,
                sl_ticks    = max(1, round(sd / self.TICK)),
                tp_ticks    = max(1, round(td / self.TICK)),
            )
        except Exception as e:
            logger.error(f"[TL] 下單失敗: {e}")
            return

        self.active_trade = ActiveTrade(
            side=side, entry_price=entry,
            sl_price=sl, tp_price=tp,
            tl_direction=sig["tl_dir"],
            order_id=oid)
        self._daily_trades += 1

        logger.info(f"[TL] {'多' if side=='long' else '空'} @ {entry:.2f} "
                    f"SL={sl:.2f} TP={tp:.2f} RR={td/sd:.1f} R²={sig['r2']:.3f}")

        if self.notifier and not getattr(self.trading_service, '_is_backtest', False):
            try:
                await self.notifier.send_message(
                    f"📈 *TL 進場*\n{'多' if side=='long' else '空'} @ {entry:.2f}\n"
                    f"SL={sl:.2f} TP={tp:.2f} RR={td/sd:.1f} R²={sig['r2']:.3f}")
            except Exception:
                pass
