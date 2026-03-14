# strategies/ob_strategy.py
"""
OB Pullback Strategy — 訂單塊回撤策略（加速版）
================================================
加速優化：
  1. on_bar 內部改為同步執行（省去 asyncio 協程開銷）
  2. OB 識別向量化（numpy，不逐根 Python 迴圈）
  3. 時段/趨勢/持倉 early-exit，無效 bar 幾乎零成本
  4. OB 查重用 set 取代 list 線性搜尋
  5. ATR 每根快取，不重複計算
  6. Timestamp 只在必要時建立
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime

from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from core.data_hub import DataHub
from utils.logger import logger


# ──────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────

@dataclass
class OBZone:
    side:         str    # "bullish" | "bearish"
    top:          float
    bottom:       float
    bar_idx:      int    # 建立時的全域 bar_count
    thrust_high:  float
    thrust_low:   float
    atr:          float
    used:         bool = False

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class ActiveTrade:
    side:                str
    entry_price:         float
    sl_price:            float
    tp_price:            float
    partial_closed:      bool  = False
    trailing_active:     bool  = False
    trailing_sl:         float = 0.0
    trailing_last_moved: float = 0.0
    is_closed:           bool  = False
    order_id:            Optional[int] = None


# ──────────────────────────────────────────
# 主策略
# ──────────────────────────────────────────

class OBStrategy(BaseStrategy):

    BACKTEST_PARAMS = {
        "htf_ema":             200,
        "htf_timeframe":       "1h",
        "ob_thrust_atr_mult":  1.5,
        "ob_thrust_bars":      3,
        "ob_max_age_bars":     40,
        "ob_vol_ratio":        0.7,
        "pin_bar_ratio":       2.0,
        "require_signal":      False,
        "min_rr":              1.5,
        "sl_buffer_mult":      0.3,
        "sl_max_atr_mult":     1.5,
        "partial_tp_r":        1.5,
        "partial_close_ratio": 0.5,
        "trailing_r":          1.5,
        "atr_period":          14,
        "vol_lookback":        20,
        "max_daily_trades":    3,
        "daily_loss_limit_r":  2.0,
        "atr_env_lookback":    1000,
        "atr_env_min":         0.0,
        "session_london_start":    7,
        "session_london_end":      12,
        "session_ny_start":        13,
        "session_ny_start_min":    30,
        "session_ny_end":          20,
    }

    def __init__(self, trading_service, market_service, notifier, engine,
                 htf_ema: int = 200,
                 htf_timeframe: str = "1h",
                 ob_thrust_atr_mult: float = 1.5,
                 ob_thrust_bars: int = 3,
                 ob_max_age_bars: int = 40,
                 ob_vol_ratio: float = 0.7,
                 pin_bar_ratio: float = 2.0,
                 require_signal: bool = False,
                 min_rr: float = 1.5,
                 sl_buffer_mult: float = 0.3,
                 sl_max_atr_mult: float = 1.5,
                 partial_tp_r: float = 1.5,
                 partial_close_ratio: float = 0.5,
                 trailing_r: float = 1.5,
                 atr_period: int = 14,
                 vol_lookback: int = 20,
                 max_daily_trades: int = 3,
                 daily_loss_limit_r: float = 2.0,
                 atr_env_lookback: int = 1000,
                 atr_env_min: float = 0.0,
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
        self.ob_thrust_atr_mult = ob_thrust_atr_mult
        self.ob_thrust_bars     = ob_thrust_bars
        self.ob_max_age_bars    = ob_max_age_bars
        self.ob_vol_ratio       = ob_vol_ratio
        self.pin_bar_ratio      = pin_bar_ratio
        self.require_signal     = require_signal
        self.min_rr             = min_rr
        self.sl_buffer_mult     = sl_buffer_mult
        self.sl_max_atr_mult    = sl_max_atr_mult
        self.partial_tp_r       = partial_tp_r
        self.partial_close_ratio = partial_close_ratio
        self.trailing_r         = trailing_r
        self.atr_period         = atr_period
        self.vol_lookback       = vol_lookback
        self.max_daily_trades   = max_daily_trades
        self.daily_loss_limit_r = daily_loss_limit_r
        self.atr_env_lookback   = atr_env_lookback
        self.atr_env_min        = atr_env_min
        self.session_london_start  = session_london_start
        self.session_london_end    = session_london_end
        self.session_ny_start      = session_ny_start
        self.session_ny_start_min  = session_ny_start_min
        self.session_ny_end        = session_ny_end

        self.TICK            = 0.25
        self.TICKS_PER_POINT = 4
        self.account_id: int = 0

        # 狀態
        self.active_trade: Optional[ActiveTrade] = None
        self._ob_zones:    List[OBZone] = []
        self._ob_idx_set:  set = set()

        self._bar_count:   int = 0

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
            "session": 0, "htf_neutral": 0, "no_ob": 0,
            "no_signal": 0, "sl_too_large": 0, "rr_too_low": 0,
            "daily_limit": 0, "atr_env": 0,
        }

        self.dashboard: dict = {
            "session_open": False,
            "status": "初始化中...",
            "active_trade": None,
        }

    # ──────────────────────────────────────────────────────────────────
    # 主流程
    # ──────────────────────────────────────────────────────────────────

    async def on_bar(self, symbol: str, df) -> None:
        """
        async 介面。早退判斷全部同步，只有最後下單才 await。
        回測場景：35萬根 bar 中只有幾千次真正進場，await 開銷極小。
        """
        if symbol != self.contract_id or len(df) < 50:
            return

        self._bar_count += 1

        # 預先取出 numpy arrays（後續全部用 numpy，不走 pandas）
        c_arr = df['c'].values
        o_arr = df['o'].values
        h_arr = df['h'].values
        l_arr = df['l'].values
        v_arr = df['v'].values if 'v' in df else np.ones(len(c_arr))

        c_last = float(c_arr[-1])
        o_last = float(o_arr[-1])
        h_last = float(h_arr[-1])
        l_last = float(l_arr[-1])

        # ── 1. 時段過濾 ───────────────────────────────────────────────
        bt = df.index[-1]
        if not self._is_session_open_ts(bt):
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
            self._manage_trade_sync(c_last, h_last, l_last)
            return

        # ── 5. ATR 快取 ───────────────────────────────────────────────
        if self._atr_bar != self._bar_count:
            self._cached_atr = self._calc_atr(h_arr, l_arr, c_arr)
            self._atr_bar    = self._bar_count
        atr = self._cached_atr
        if atr <= 0:
            return


        # ── 5b. ATR 環境過濾（低波動市場不交易）──────────────────────────
        if self.atr_env_min > 0:
            env_atr = self._calc_env_atr(h_arr, l_arr, c_arr)
            if env_atr < self.atr_env_min:
                self.skip_counts["atr_env"] += 1
                return

        # ── 6. HTF 趨勢 ───────────────────────────────────────────────
        trend = self._get_htf_trend(df, c_arr)
        if trend == "neutral":
            self.skip_counts["htf_neutral"] += 1
            return

        # ── 7. 更新 OB ────────────────────────────────────────────────
        self._update_ob(o_arr, h_arr, l_arr, c_arr, atr)

        if not self._ob_zones:
            self.skip_counts["no_ob"] += 1
            return

        # ── 8. 找進場訊號 ─────────────────────────────────────────────
        entry_side = "bullish" if trend == "bull" else "bearish"
        vl = float(v_arr[-1])
        va = float(np.mean(v_arr[-self.vol_lookback:])) \
             if len(v_arr) >= self.vol_lookback else float(np.mean(v_arr))

        signal = self._find_entry(entry_side, c_last, o_last, h_last, l_last,
                                   vl, va, atr, o_arr, c_arr)
        if signal is None:
            return

        # ── 9. 下單（只有真正進場才 await，35萬根裡只發生幾千次）────────
        await self._place(signal)

    # ──────────────────────────────────────────────────────────────────
    # 時段
    # ──────────────────────────────────────────────────────────────────

    def _is_session_open_ts(self, bt) -> bool:
        try:
            h = bt.hour;  m = bt.minute
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
        p = self.atr_period
        if len(h) < p + 1:
            return float(np.mean(h - l)) if len(h) > 0 else 1.0
        h2 = h[-(p+1):-1];  h3 = h[-p:]
        l2 = l[-(p+1):-1];  l3 = l[-p:]
        c2 = c[-(p+1):-1]
        tr = np.maximum(h3 - l3, np.maximum(np.abs(h3 - c2), np.abs(l3 - c2)))
        return float(np.mean(tr))


    def _calc_env_atr(self, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> float:
        """
        環境 ATR：過去 atr_env_lookback 根 bar 的 TR 均值。
        代表近期市場整體波動水準，用來過濾低波動環境。
        """
        n = min(len(h), self.atr_env_lookback)
        if n < 2:
            return 999.0
        h2 = h[-n:]; l2 = l[-n:]; c2 = c[-n:]
        tr = np.maximum(h2[1:]-l2[1:],
                        np.maximum(np.abs(h2[1:]-c2[:-1]), np.abs(l2[1:]-c2[:-1])))
        return float(np.mean(tr))

    @staticmethod
    def _ema(closes: np.ndarray, period: int) -> np.ndarray:
        k = 2.0 / (period + 1)
        e = np.empty(len(closes))
        e[0] = closes[0]
        for i in range(1, len(closes)):
            e[i] = closes[i] * k + e[i-1] * (1 - k)
        return e

    # ──────────────────────────────────────────────────────────────────
    # HTF 趨勢
    # ──────────────────────────────────────────────────────────────────

    def _get_htf_trend(self, df, c_arr: np.ndarray) -> str:
        # 回測：查預算表（O(log n)，幾乎零成本）
        pc = self._htf_precomputed
        if pc:
            key   = f"{self.htf_timeframe}_{self.htf_ema}"
            entry = pc.get(key)
            if entry:
                st, sd = entry
                bs        = getattr(self, '_backtest_bar_store', None)
                cutoff_ns = getattr(bs, 'current_bar_time_ns', None) if bs else None
                if cutoff_ns is not None:
                    idx = int(np.searchsorted(st, cutoff_ns, side='right')) - 1
                    if 0 <= idx < len(sd):
                        return sd[idx]

        # 即時：從 bar_store 讀 HTF
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

        # Fallback：用 5M 本身估算
        if len(c_arr) >= self.htf_ema:
            e = self._ema(c_arr, self.htf_ema)
            c, ev = c_arr[-1], e[-1]
            if c > ev * 1.001: return "bull"
            if c < ev * 0.999: return "bear"
        return "neutral"

    # ──────────────────────────────────────────────────────────────────
    # OB 識別（向量化）
    # ──────────────────────────────────────────────────────────────────

    def _update_ob(self, o: np.ndarray, h: np.ndarray,
                   l: np.ndarray, c: np.ndarray, atr: float) -> None:
        """
        加速版：每根 bar 只檢查「以當前這根為推進終點」的新 OB。
        
        原理：推進終點 = 當前根(n-1)，推進起點 = n-tb，OB候選 = n-tb-1。
        每根 bar 只產生一個新的推進終點，所以每次只掃一個位置。
        歷史 OB 在它們被建立的那根 bar 時已經掃過了，不會漏掉。
        """
        tb = self.ob_thrust_bars
        n  = len(c)
        if n < tb + 2:
            return

        # 清除過期 / 已用
        cutoff = self._bar_count - self.ob_max_age_bars
        if self._ob_zones:
            kept = [z for z in self._ob_zones if not z.used and z.bar_idx >= cutoff]
            self._ob_zones   = kept
            self._ob_idx_set = {z.bar_idx for z in kept}

        tmin = self.ob_thrust_atr_mult * atr

        # 以當前根(n-1)為推進終點，推進起點是 n-tb，OB候選是 n-tb-1
        end_idx   = n - 1       # 推進終點（當前根）
        start_idx = n - tb      # 推進起點
        ob_idx    = n - tb - 1  # OB 候選（推進起點前一根）

        if ob_idx < 0:
            return

        # 取需要的片段（多取 5 根給 prev_high/low 用）
        fetch_from = max(0, ob_idx - 4)
        os_ = o[fetch_from: end_idx + 1]
        hs_ = h[fetch_from: end_idx + 1]
        ls_ = l[fetch_from: end_idx + 1]
        cs_ = c[fetch_from: end_idx + 1]

        # 局部索引（相對於 fetch_from）
        ob_l    = ob_idx    - fetch_from
        start_l = start_idx - fetch_from
        end_l   = end_idx   - fetch_from

        is_bull = cs_ > os_
        is_bear = cs_ < os_

        # 全域 gidx（OB候選對應的全域 bar_count 位置）
        gidx = self._bar_count - (n - 1 - ob_idx)

        if gidx in self._ob_idx_set or gidx < cutoff:
            return

        # 做多 OB：推進全陽 + OB 是陰線
        if np.all(is_bull[start_l: end_l + 1]) and is_bear[ob_l]:
            thrust_range = hs_[end_l] - ls_[start_l]
            if thrust_range >= tmin:
                ph = float(np.max(hs_[max(0, ob_l - 4): ob_l + 1]))
                if hs_[end_l] > ph:
                    top = max(os_[ob_l], cs_[ob_l])
                    bot = min(os_[ob_l], cs_[ob_l])
                    if top > bot:
                        self._ob_zones.append(OBZone(
                            side="bullish", top=top, bottom=bot,
                            bar_idx=gidx,
                            thrust_high=float(hs_[end_l]),
                            thrust_low=float(ls_[start_l]),
                            atr=atr))
                        self._ob_idx_set.add(gidx)

        # 做空 OB：推進全陰 + OB 是陽線
        if np.all(is_bear[start_l: end_l + 1]) and is_bull[ob_l]:
            thrust_range = hs_[start_l] - ls_[end_l]
            if thrust_range >= tmin:
                pl = float(np.min(ls_[max(0, ob_l - 4): ob_l + 1]))
                if ls_[end_l] < pl:
                    top = max(os_[ob_l], cs_[ob_l])
                    bot = min(os_[ob_l], cs_[ob_l])
                    if top > bot:
                        self._ob_zones.append(OBZone(
                            side="bearish", top=top, bottom=bot,
                            bar_idx=gidx,
                            thrust_high=float(hs_[start_l]),
                            thrust_low=float(ls_[end_l]),
                            atr=atr))
                        self._ob_idx_set.add(gidx)

    # ──────────────────────────────────────────────────────────────────
    # 進場訊號
    # ──────────────────────────────────────────────────────────────────

    def _find_entry(self, side: str, c: float, o: float, h: float, l: float,
                     vl: float, va: float, atr: float,
                     o_arr: np.ndarray, c_arr: np.ndarray) -> Optional[dict]:

        for ob in self._ob_zones:
            if ob.used or ob.side != side:
                continue

            if side == "bullish":
                if not (ob.bottom <= c <= ob.top * 1.002):
                    continue
                if c < ob.bottom - atr * 0.1:
                    continue

                if self.require_signal:
                    ok = (self._pbar_bull(o, h, l, c)
                          or self._engulf_bull(o_arr, c_arr)
                          or (self.ob_vol_ratio > 0 and va > 0
                              and vl < va * self.ob_vol_ratio))
                    if not ok:
                        self.skip_counts["no_signal"] += 1
                        continue

                sl = ob.bottom - atr * self.sl_buffer_mult
                sd = c - sl
                if sd <= 0: continue
                if sd > atr * self.sl_max_atr_mult:
                    self.skip_counts["sl_too_large"] += 1
                    continue

                tp_f = c + sd * self.min_rr
                tp   = min(tp_f * 1.2, ob.thrust_high) \
                       if ob.thrust_high > tp_f else tp_f
                if (tp - c) / sd < self.min_rr:
                    self.skip_counts["rr_too_low"] += 1
                    continue

                ob.used = True
                self._ob_idx_set.discard(ob.bar_idx)
                return {"side": "long", "entry": c, "sl": sl, "tp": tp, "atr": atr}

            else:  # bearish
                if not (ob.bottom * 0.998 <= c <= ob.top):
                    continue
                if c > ob.top + atr * 0.1:
                    continue

                if self.require_signal:
                    ok = (self._pbar_bear(o, h, l, c)
                          or self._engulf_bear(o_arr, c_arr)
                          or (self.ob_vol_ratio > 0 and va > 0
                              and vl < va * self.ob_vol_ratio))
                    if not ok:
                        self.skip_counts["no_signal"] += 1
                        continue

                sl = ob.top + atr * self.sl_buffer_mult
                sd = sl - c
                if sd <= 0: continue
                if sd > atr * self.sl_max_atr_mult:
                    self.skip_counts["sl_too_large"] += 1
                    continue

                tp_f = c - sd * self.min_rr
                tp   = max(tp_f * 0.8, ob.thrust_low) \
                       if ob.thrust_low < tp_f else tp_f
                if (c - tp) / sd < self.min_rr:
                    self.skip_counts["rr_too_low"] += 1
                    continue

                ob.used = True
                self._ob_idx_set.discard(ob.bar_idx)
                return {"side": "short", "entry": c, "sl": sl, "tp": tp, "atr": atr}

        self.skip_counts["no_ob"] += 1
        return None

    # ──────────────────────────────────────────────────────────────────
    # K棒形態（純 Python 數值，無 pandas）
    # ──────────────────────────────────────────────────────────────────

    def _pbar_bull(self, o, h, l, c) -> bool:
        body = abs(c - o)
        shad = (c if c > o else o) - l
        return body > 0 and shad >= body * self.pin_bar_ratio and c > (l + h) * 0.5

    def _pbar_bear(self, o, h, l, c) -> bool:
        body = abs(c - o)
        shad = h - (c if c < o else o)
        return body > 0 and shad >= body * self.pin_bar_ratio and c < (l + h) * 0.5

    def _engulf_bull(self, o_arr, c_arr) -> bool:
        if len(o_arr) < 2: return False
        po, pc = o_arr[-2], c_arr[-2]
        co, cc = o_arr[-1], c_arr[-1]
        return pc < po and cc > co and co < pc and cc > po

    def _engulf_bear(self, o_arr, c_arr) -> bool:
        if len(o_arr) < 2: return False
        po, pc = o_arr[-2], c_arr[-2]
        co, cc = o_arr[-1], c_arr[-1]
        return pc > po and cc < co and co > pc and cc < po

    # ──────────────────────────────────────────────────────────────────
    # 持倉管理（同步）
    # ──────────────────────────────────────────────────────────────────

    def _manage_trade_sync(self, c: float, h: float, l: float) -> None:
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
                nsl = c - atr * 0.5
                if nsl > t.trailing_sl and nsl > t.sl_price:
                    t.trailing_sl = nsl
                    self._move_sl(t, nsl)
            else:
                nsl = c + atr * 0.5
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
        side   = sig["side"]
        entry  = sig["entry"]
        sl     = sig["sl"]
        tp     = sig["tp"]
        atr    = sig["atr"]
        sd     = abs(entry - sl)
        td     = abs(tp - entry)

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
            logger.error(f"[OB] 下單失敗: {e}")
            return

        self.active_trade = ActiveTrade(
            side=side, entry_price=entry,
            sl_price=sl, tp_price=tp, order_id=oid)
        self._daily_trades += 1

        logger.info(f"[OB] {'多' if side=='long' else '空'} @ {entry:.2f} "
                    f"SL={sl:.2f} TP={tp:.2f} RR={td/sd:.1f}")

        if self.notifier and not getattr(self.trading_service, '_is_backtest', False):
            try:
                await self.notifier.send_message(
                    f"🟢 *OB 進場*\n{'多' if side=='long' else '空'} @ {entry:.2f}\n"
                    f"SL={sl:.2f} TP={tp:.2f} RR={td/sd:.1f}")
            except Exception:
                pass
