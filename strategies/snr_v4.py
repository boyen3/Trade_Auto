# strategies/snr_v4.py
"""
SNR Strategy v4
===============
大格局支撐壓力 + 小格局假突破反轉

核心邏輯：
  1. SR 識別：4H + 1H + 15M 三時框，強度分級
  2. 趨勢：1H EMA50 決定方向（bull/bear）
  3. 進場：5M 在 SR 區出現假突破 + 反轉K棒 + 量能確認
  4. 止損：假突破極值 + 緩衝
  5. 止盈：第1口固定 RR，第2口以上追蹤止損

15M 趨勢過濾：由參數 mtf_mode 控制
  "off"      → 不看 15M（基準）
  "pullback" → 15M bear/neutral 才做多，bull/neutral 才做空（回調進場）
  "breakout" → 15M bull 才做多，bear 才做空（突破回測進場）
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime

from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from utils.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SRZone:
    top:       float
    bottom:    float
    strength:  float
    timeframe: str
    touch_count: int = 1

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class ActiveTrade:
    side:            str    # "long" | "short"
    entry_price:     float
    sl_price:        float
    initial_sl:      float  # 原始止損，不隨移動止損更新（用來算 R）
    tp1_price:       float  # 第 1 口止盈
    n_contracts:     int    # 總口數
    contracts_left:  int    # 剩餘口數
    tp1_done:        bool  = False
    trailing_sl:     float = 0.0
    trailing_active: bool  = False
    trailing_last:   float = 0.0
    is_closed:       bool  = False
    order_id:        Optional[int] = None


# ──────────────────────────────────────────────────────────────────────────────
# 主策略
# ──────────────────────────────────────────────────────────────────────────────

class SNRStrategyV4(BaseStrategy):

    BACKTEST_PARAMS = {
        # ── SR 識別 ────────────────────────────────────────
        "sr_lookback_15m":    500,    # 15M 回望根數（約 2.5 天）
        "sr_lookback_1h":     300,    # 1H  回望根數（約 12 天）
        "sr_lookback_4h":     200,    # 4H  回望根數（約 33 天）
        "sr_zone_atr_mult":   0.10,   # 格寬 = ATR × 此值
        "sr_sensitivity":     1.5,    # 門檻 = 均密度 × 此值
        "sr_body_weight":     0.7,    # K棒實體佔密度權重
        "sr_max_zones":       12,     # 每時框最多保留幾個 SR 區
        "sr_merge_dist_mult": 0.5,    # 跨時框合併距離 = ATR × 此值
        "sr_min_strength":    1,      # 最低強度：1=弱以上 2=中以上 3=強
        "sr_update_bars":     72,     # 每幾根 5M bar 重算 SR（72=6小時）

        # ── 趨勢確認 ───────────────────────────────────────
        "htf_ema":            50,     # 1H EMA 週期
        "mtf_ema":            21,     # 15M EMA 週期
        "mtf_mode":           "off",  # "off" | "pullback" | "breakout"

        # ── 進場條件 ───────────────────────────────────────
        "sr_near_mult":       1.5,    # 靠近 SR = 距邊緣 ≤ ATR × 此值
        "vol_mult":           1.2,    # 量能門檻 = 均量 × 此值
        "vol_lookback":       20,     # 均量計算根數
        "require_pin_bar":    True,   # 是否要求 Pin Bar / 吞噬
        "pin_bar_ratio":      1.5,    # 影線 ≥ 實體 × 此值

        # ── 止損 ───────────────────────────────────────────
        "sl_buffer_mult":     0.15,   # 止損緩衝 = ATR × 此值
        "sl_max_atr_mult":    1.0,    # 止損距離上限 = ATR × 此值

        # ── 止盈 ───────────────────────────────────────────
        "min_rr":             1.5,    # 第 1 口固定止盈 RR
        "trail_atr_fast":     0.5,    # 第 2 口追蹤止損距離（ATR 倍數）
        "trail_atr_slow":     1.0,    # 第 3 口追蹤止損距離（ATR 倍數）
        "n_contracts":        2,      # 總口數（1=只有固定RR，2+=有追蹤）

        # ── 時段 ───────────────────────────────────────────
        "session_london_start":  7,
        "session_london_end":    12,
        "session_ny_start":      13,
        "session_ny_start_min":  30,
        "session_ny_end":        20,

        # ── 日內風控 ───────────────────────────────────────
        "max_daily_trades":   5,
        "daily_loss_r":       2.0,
        "atr_period":         14,
    }

    def __init__(self, trading_service, market_service, notifier, engine,
                 sr_lookback_15m: int   = 500,
                 sr_lookback_1h: int    = 300,
                 sr_lookback_4h: int    = 200,
                 sr_zone_atr_mult: float= 0.10,
                 sr_sensitivity: float  = 1.5,
                 sr_body_weight: float  = 0.7,
                 sr_max_zones: int      = 12,
                 sr_merge_dist_mult: float = 0.5,
                 sr_min_strength: int   = 1,
                 sr_update_bars: int    = 72,
                 htf_ema: int           = 50,
                 mtf_ema: int           = 21,
                 mtf_mode: str          = "off",
                 sr_near_mult: float    = 1.5,
                 vol_mult: float        = 1.2,
                 vol_lookback: int      = 20,
                 require_pin_bar: bool  = True,
                 pin_bar_ratio: float   = 1.5,
                 sl_buffer_mult: float  = 0.15,
                 sl_max_atr_mult: float = 1.0,
                 min_rr: float          = 1.5,
                 trail_atr_fast: float  = 0.5,
                 trail_atr_slow: float  = 1.0,
                 n_contracts: int       = 2,
                 session_london_start: int = 7,
                 session_london_end: int   = 12,
                 session_ny_start: int     = 13,
                 session_ny_start_min: int = 30,
                 session_ny_end: int       = 20,
                 max_daily_trades: int  = 5,
                 daily_loss_r: float    = 2.0,
                 atr_period: int        = 14,
                 contract_id: str       = "CON.F.US.MNQ.H26",
                 **kwargs):

        super().__init__(trading_service, market_service)
        self.notifier    = notifier
        self.engine      = engine
        self.contract_id = contract_id
        self.account_id  = 0

        # SR
        self.sr_lookback       = {"15m": sr_lookback_15m,
                                   "1h":  sr_lookback_1h,
                                   "4h":  sr_lookback_4h}
        self.sr_zone_atr_mult  = sr_zone_atr_mult
        self.sr_sensitivity    = sr_sensitivity
        self.sr_body_weight    = sr_body_weight
        self.sr_max_zones      = sr_max_zones
        self.sr_merge_dist_mult= sr_merge_dist_mult
        self.sr_min_strength   = sr_min_strength
        self.sr_update_bars    = sr_update_bars

        # 趨勢
        self.htf_ema   = htf_ema
        self.mtf_ema   = mtf_ema
        self.mtf_mode  = mtf_mode

        # 進場
        self.sr_near_mult    = sr_near_mult
        self.vol_mult        = vol_mult
        self.vol_lookback    = vol_lookback
        self.require_pin_bar = require_pin_bar
        self.pin_bar_ratio   = pin_bar_ratio

        # 止損止盈
        self.sl_buffer_mult  = sl_buffer_mult
        self.sl_max_atr_mult = sl_max_atr_mult
        self.min_rr          = min_rr
        self.trail_atr_fast  = trail_atr_fast
        self.trail_atr_slow  = trail_atr_slow
        self.n_contracts     = n_contracts

        # 時段
        self.session_london_start  = session_london_start
        self.session_london_end    = session_london_end
        self.session_ny_start      = session_ny_start
        self.session_ny_start_min  = session_ny_start_min
        self.session_ny_end        = session_ny_end

        # 風控
        self.max_daily_trades = max_daily_trades
        self.daily_loss_r     = daily_loss_r
        self.atr_period       = atr_period
        self.TICK             = 0.25

        # 狀態
        self.active_trade:       Optional[ActiveTrade] = None
        self._cached_sr_zones:   List[SRZone] = []
        self._sr_cache_bar:      int   = -9999
        self._bar_count:         int   = 0
        self._cached_atr:        float = 10.0
        self._atr_bar:           int   = -1
        self._current_day        = None
        self._daily_trades:      int   = 0
        self._daily_pnl_r:       float = 0.0

        # HTF 預算（回測引擎注入）
        self._htf_precomputed = None
        self._backtest_bar_store = None

        # Skip 統計
        self.skip_counts = {
            "session": 0, "htf_neutral": 0, "mtf_filter": 0,
            "no_sr": 0, "no_entry": 0, "vol_filter": 0,
            "no_pinbar": 0, "sl_too_large": 0, "rr_too_low": 0,
            "daily_limit": 0,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 主流程
    # ──────────────────────────────────────────────────────────────────────────

    async def on_bar(self, symbol: str, df) -> None:
        if symbol != self.contract_id or len(df) < 50:
            return

        self._bar_count += 1

        c_arr = df['c'].values
        o_arr = df['o'].values if 'o' in df.columns else c_arr
        h_arr = df['h'].values
        l_arr = df['l'].values
        v_arr = df['v'].values if 'v' in df.columns else np.ones(len(c_arr))

        c = float(c_arr[-1])
        h = float(h_arr[-1])
        l = float(l_arr[-1])
        o = float(o_arr[-1])

        # 1. 時段過濾
        bt = df.index[-1]
        if not self._is_session_open(bt):
            self.skip_counts["session"] += 1
            return

        # 2. 日內重置
        self._reset_daily(bt)

        # 3. 日內風控
        if self._daily_trades >= self.max_daily_trades or \
           self._daily_pnl_r  <= -self.daily_loss_r:
            self.skip_counts["daily_limit"] += 1
            return

        # 4. ATR
        atr = self._calc_atr(h_arr, l_arr, c_arr)
        if atr <= 0:
            return

        # 5. 管理持倉
        if self.active_trade:
            self._manage_trade(c, h, l, atr)
            return

        # 6. 1H 趨勢
        trend = self._get_trend("1h", self.htf_ema)
        if trend == "neutral":
            self.skip_counts["htf_neutral"] += 1
            return

        # 7. 15M 趨勢過濾（依 mtf_mode）
        if self.mtf_mode != "off":
            mtf = self._get_trend("15m", self.mtf_ema)
            if self.mtf_mode == "pullback":
                # 做多需 15M bear 或 neutral（回調中）
                # 做空需 15M bull 或 neutral（反彈中）
                if trend == "bull" and mtf == "bull":
                    self.skip_counts["mtf_filter"] += 1
                    return
                if trend == "bear" and mtf == "bear":
                    self.skip_counts["mtf_filter"] += 1
                    return
            elif self.mtf_mode == "breakout":
                # 做多需 15M bull（突破回測）
                # 做空需 15M bear（突破回測）
                if trend == "bull" and mtf != "bull":
                    self.skip_counts["mtf_filter"] += 1
                    return
                if trend == "bear" and mtf != "bear":
                    self.skip_counts["mtf_filter"] += 1
                    return

        # 8. 更新 SR（每 sr_update_bars 根重算）
        if self._bar_count - self._sr_cache_bar >= self.sr_update_bars:
            self._update_sr(df, atr)
            self._sr_cache_bar = self._bar_count

        if not self._cached_sr_zones:
            self.skip_counts["no_sr"] += 1
            return

        # 9. 找進場
        signal = self._find_entry(
            c, h, l, o, c_arr, h_arr, l_arr, o_arr, v_arr, atr, trend
        )
        if signal is None:
            return

        # 10. 下單
        await self._place(signal, atr)

    # ──────────────────────────────────────────────────────────────────────────
    # 時段 / 日內
    # ──────────────────────────────────────────────────────────────────────────

    def _is_session_open(self, bt) -> bool:
        try:
            hm = bt.hour * 60 + bt.minute
            if self.session_london_start * 60 <= hm < self.session_london_end * 60:
                return True
            ny0 = self.session_ny_start * 60 + self.session_ny_start_min
            if ny0 <= hm < self.session_ny_end * 60:
                return True
        except Exception:
            return True
        return False

    def _reset_daily(self, bt) -> None:
        try:
            today = bt.date()
            if today != self._current_day:
                self._current_day  = today
                self._daily_trades = 0
                self._daily_pnl_r  = 0.0
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # ATR
    # ──────────────────────────────────────────────────────────────────────────

    def _calc_atr(self, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> float:
        if self._atr_bar == self._bar_count:
            return self._cached_atr
        p = self.atr_period
        if len(h) < p + 1:
            v = float(np.mean(h - l)) if len(h) > 0 else 1.0
        else:
            tr = np.maximum(h[-p:] - l[-p:],
                 np.maximum(np.abs(h[-p:] - c[-(p+1):-1]),
                            np.abs(l[-p:] - c[-(p+1):-1])))
            v = float(tr.mean())
        self._cached_atr = v
        self._atr_bar    = self._bar_count
        return v

    # ──────────────────────────────────────────────────────────────────────────
    # 趨勢
    # ──────────────────────────────────────────────────────────────────────────

    def _get_trend(self, timeframe: str, ema_period: int) -> str:
        """查預算表（回測）或動態計算（即時）"""
        try:
            pc = self._htf_precomputed
            if pc:
                key   = f"{timeframe}_{ema_period}"
                entry = pc.get(key)
                if entry:
                    times, dirs = entry
                    bs = self._backtest_bar_store
                    cutoff_ns = getattr(bs, 'current_bar_time_ns', None) if bs else None
                    if cutoff_ns is not None:
                        pos = int(np.searchsorted(times, np.int64(cutoff_ns),
                                                  side='right')) - 1
                        if 0 <= pos < len(dirs):
                            return dirs[pos]

            # 即時模式：動態計算
            bs = getattr(self, '_backtest_bar_store', None) or \
                 getattr(self.engine, 'bar_store', None)
            if bs is None:
                return "neutral"
            df = bs.load(self.contract_id, timeframe, limit=ema_period * 3)
            if df is None or len(df) < ema_period + 5:
                return "neutral"
            closes = df['c'].values
            k = 2.0 / (ema_period + 1)
            ema = closes[0]
            for cv in closes[1:]:
                ema = cv * k + ema * (1 - k)
            margin = ema * 0.001
            if closes[-1] > ema + margin: return "bull"
            if closes[-1] < ema - margin: return "bear"
        except Exception:
            pass
        return "neutral"

    # ──────────────────────────────────────────────────────────────────────────
    # SR 識別（複用 v3 密度法核心）
    # ──────────────────────────────────────────────────────────────────────────

    def _calc_sr_density(self, h: np.ndarray, l: np.ndarray,
                          o: np.ndarray, c: np.ndarray,
                          v: np.ndarray, atr: float,
                          timeframe: str) -> List[SRZone]:
        """密度法 SR 識別（向量化）"""
        if len(h) < 10:
            return []

        zone_thickness = max(atr * self.sr_zone_atr_mult, 0.25)
        avg_vol    = np.mean(v) if np.mean(v) > 0 else 1.0
        vol_factor = 1.0 + (v / avg_vol - 1.0) * 0.3

        price_min = float(l.min())
        price_max = float(h.max())
        n_bins    = max(int((price_max - price_min) / zone_thickness) + 1, 1)

        bin_density = np.zeros(n_bins)
        bin_edges   = np.arange(n_bins + 1) * zone_thickness + price_min

        bw      = self.sr_body_weight
        sw      = 1.0 - bw
        body_lo = np.minimum(o, c)
        body_hi = np.maximum(o, c)

        def _add(lo_arr, hi_arr, w_arr):
            if len(lo_arr) == 0:
                return
            center  = (lo_arr + hi_arr) / 2
            width   = np.maximum(hi_arr - lo_arr, 0)
            w_scale = w_arr * (width / zone_thickness + 1)
            vals, _ = np.histogram(center, bins=bin_edges, weights=w_scale)
            bin_density[:len(vals)] += vals

        mask_body  = body_hi > body_lo
        mask_upper = h > body_hi
        mask_lower = l < body_lo

        if mask_body.any():
            _add(body_lo[mask_body], body_hi[mask_body], (bw * vol_factor)[mask_body])
        if mask_upper.any():
            _add(body_hi[mask_upper], h[mask_upper], (sw * vol_factor * 0.5)[mask_upper])
        if mask_lower.any():
            _add(l[mask_lower], body_lo[mask_lower], (sw * vol_factor * 0.5)[mask_lower])

        nonzero   = bin_density[bin_density > 0]
        if len(nonzero) == 0:
            return []
        threshold = np.mean(nonzero) * self.sr_sensitivity
        hot_bins  = np.where(bin_density > threshold)[0]

        zones = []
        for i in hot_bins:
            bottom = price_min + i * zone_thickness
            zones.append(SRZone(
                top=round(bottom + zone_thickness, 2),
                bottom=round(bottom, 2),
                strength=float(bin_density[i]),
                timeframe=timeframe,
            ))
        zones.sort(key=lambda z: z.strength, reverse=True)
        return zones[:self.sr_max_zones]

    def _update_sr(self, df_5m, atr: float) -> None:
        """整合 15M + 1H + 4H 三時框 SR，跨時框重疊加強"""
        all_zones: List[SRZone] = []

        # 15M：從傳入的 5M df 降採樣
        try:
            df_15m = self._resample_to_15m(df_5m)
            if df_15m is not None and len(df_15m) >= 10:
                n  = min(len(df_15m), self.sr_lookback["15m"])
                sl = df_15m.iloc[-n:]
                z  = self._calc_sr_density(
                    sl['h'].values, sl['l'].values,
                    sl['o'].values if 'o' in sl.columns else sl['c'].values,
                    sl['c'].values,
                    sl['v'].values if 'v' in sl.columns else np.ones(n),
                    atr, "15m")
                all_zones.extend(z)
        except Exception as e:
            logger.debug(f"[v4] 15M SR error: {e}")

        # 1H + 4H：從 bar_store 讀取
        bs = self._backtest_bar_store or getattr(self.engine, 'bar_store', None)
        for tf in ["1h", "4h"]:
            if bs is None:
                break
            try:
                limit = self.sr_lookback[tf]
                df_tf = bs.load(self.contract_id, tf, limit=limit)
                if df_tf is None or len(df_tf) < 10:
                    continue
                n  = len(df_tf)
                z  = self._calc_sr_density(
                    df_tf['h'].values, df_tf['l'].values,
                    df_tf['o'].values if 'o' in df_tf.columns else df_tf['c'].values,
                    df_tf['c'].values,
                    df_tf['v'].values if 'v' in df_tf.columns else np.ones(n),
                    atr, tf)
                all_zones.extend(z)
            except Exception as e:
                logger.debug(f"[v4] {tf} SR error: {e}")

        if not all_zones:
            self._cached_sr_zones = []
            return

        # 合併跨時框重疊區域
        merge_dist = atr * self.sr_merge_dist_mult
        all_zones.sort(key=lambda z: z.mid)
        merged: List[SRZone] = []
        cur = all_zones[0]

        for z in all_zones[1:]:
            if abs(z.mid - cur.mid) <= merge_dist:
                tf_bonus = 1.5 if z.timeframe != cur.timeframe else 1.0
                cur = SRZone(
                    top=round(max(cur.top, z.top), 2),
                    bottom=round(min(cur.bottom, z.bottom), 2),
                    strength=cur.strength + z.strength * tf_bonus,
                    timeframe=f"{cur.timeframe}+{z.timeframe}",
                    touch_count=cur.touch_count + 1,
                )
            else:
                merged.append(cur)
                cur = z
        merged.append(cur)

        # 依強度排序，過濾弱區
        merged.sort(key=lambda z: z.strength, reverse=True)
        self._cached_sr_zones = [
            z for z in merged[:self.sr_max_zones]
            if z.touch_count >= self.sr_min_strength
        ]

    def _resample_to_15m(self, df_5m) -> Optional[pd.DataFrame]:
        """
        5M → 15M 降採樣（手動版，相容 FastDF）。
        每 3 根 5M = 1 根 15M。
        """
        try:
            c = df_5m['c'].values
            h = df_5m['h'].values
            l = df_5m['l'].values
            o = df_5m['o'].values if 'o' in df_5m.columns else c
            v = df_5m['v'].values if 'v' in df_5m.columns else np.ones(len(c))
            n = len(c)
            # 只取能整除 3 的部分
            trim = (n // 3) * 3
            if trim < 3:
                return None
            c3 = c[:trim].reshape(-1, 3)
            h3 = h[:trim].reshape(-1, 3)
            l3 = l[:trim].reshape(-1, 3)
            o3 = o[:trim].reshape(-1, 3)
            v3 = v[:trim].reshape(-1, 3)
            o15 = o3[:, 0]
            h15 = h3.max(axis=1)
            l15 = l3.min(axis=1)
            c15 = c3[:, 2]
            v15 = v3.sum(axis=1)
            idx = df_5m.index[:trim:3]
            return pd.DataFrame(
                {'o': o15, 'h': h15, 'l': l15, 'c': c15, 'v': v15},
                index=idx
            )
        except Exception as e:
            logger.debug(f"[v4] resample error: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # 進場邏輯
    # ──────────────────────────────────────────────────────────────────────────

    def _find_entry(self, c: float, h: float, l: float, o: float,
                    c_arr, h_arr, l_arr, o_arr, v_arr,
                    atr: float, trend: str) -> Optional[dict]:

        entry_side = "bullish" if trend == "bull" else "bearish"
        max_dist   = atr * self.sr_near_mult

        # 均量
        n_vol   = min(len(v_arr), self.vol_lookback)
        avg_vol = float(np.mean(v_arr[-n_vol - 1:-1])) if n_vol > 1 else float(v_arr[-1])
        cur_vol = float(v_arr[-1])

        for zone in self._cached_sr_zones:

            if entry_side == "bullish":
                # ── 做多：支撐 SR，價格在區內或剛進入上方 ──────────────
                in_zone   = zone.bottom <= c <= zone.top
                near_zone = c > zone.top and (c - zone.top) <= max_dist

                if not (in_zone or near_zone):
                    continue

                # 假突破：低點刺穿 SR 下緣，但收盤在下緣以上
                if not (l < zone.bottom and c >= zone.bottom):
                    self.skip_counts["no_entry"] += 1
                    continue

                # 量能確認
                if self.vol_mult > 0 and cur_vol < avg_vol * self.vol_mult:
                    self.skip_counts["vol_filter"] += 1
                    continue

                # 反轉 K 棒（Pin Bar 或吞噬）
                if self.require_pin_bar:
                    if not (self._is_pin_bar(o, h, l, c, "bull") or
                            self._is_engulfing(o_arr, c_arr, "bull")):
                        self.skip_counts["no_pinbar"] += 1
                        continue

                # 計算止損
                sl    = l - atr * self.sl_buffer_mult
                sd    = c - sl
                if sd <= 0 or sd > atr * self.sl_max_atr_mult:
                    self.skip_counts["sl_too_large"] += 1
                    continue

                tp1 = c + sd * self.min_rr
                return {"side": "long", "entry": c, "sl": sl,
                        "tp1": tp1, "sd": sd, "zone": zone}

            else:
                # ── 做空：壓力 SR，價格在區內或剛進入下方 ──────────────
                in_zone   = zone.bottom <= c <= zone.top
                near_zone = c < zone.bottom and (zone.bottom - c) <= max_dist

                if not (in_zone or near_zone):
                    continue

                # 假突破：高點刺穿 SR 上緣，但收盤在上緣以下
                if not (h > zone.top and c <= zone.top):
                    self.skip_counts["no_entry"] += 1
                    continue

                # 量能確認
                if self.vol_mult > 0 and cur_vol < avg_vol * self.vol_mult:
                    self.skip_counts["vol_filter"] += 1
                    continue

                # 反轉 K 棒
                if self.require_pin_bar:
                    if not (self._is_pin_bar(o, h, l, c, "bear") or
                            self._is_engulfing(o_arr, c_arr, "bear")):
                        self.skip_counts["no_pinbar"] += 1
                        continue

                # 計算止損
                sl = h + atr * self.sl_buffer_mult
                sd = sl - c
                if sd <= 0 or sd > atr * self.sl_max_atr_mult:
                    self.skip_counts["sl_too_large"] += 1
                    continue

                tp1 = c - sd * self.min_rr
                return {"side": "short", "entry": c, "sl": sl,
                        "tp1": tp1, "sd": sd, "zone": zone}

        return None

    def _is_pin_bar(self, o: float, h: float, l: float,
                    c: float, side: str) -> bool:
        body = abs(c - o)
        if body < 1e-9:
            body = (h - l) * 0.1
        if side == "bull":
            shadow = min(o, c) - l
        else:
            shadow = h - max(o, c)
        return shadow >= body * self.pin_bar_ratio

    def _is_engulfing(self, o_arr, c_arr, side: str) -> bool:
        """吞噬形態：當根實體完全吞噬前一根實體"""
        if len(o_arr) < 2:
            return False
        po, pc = float(o_arr[-2]), float(c_arr[-2])
        co, cc = float(o_arr[-1]), float(c_arr[-1])
        prev_lo, prev_hi = min(po, pc), max(po, pc)
        cur_lo,  cur_hi  = min(co, cc), max(co, cc)
        if side == "bull":
            # 前根是陰線，當根是陽線且完全吞噬
            return pc < po and cc > co and cur_lo < prev_lo and cur_hi > prev_hi
        else:
            return pc > po and cc < co and cur_lo < prev_lo and cur_hi > prev_hi

    # ──────────────────────────────────────────────────────────────────────────
    # 持倉管理
    # ──────────────────────────────────────────────────────────────────────────

    def _manage_trade(self, c: float, h: float, l: float, atr: float) -> None:
        t = self.active_trade
        if t is None or t.is_closed:
            self.active_trade = None
            return

        sd = abs(t.entry_price - t.initial_sl)
        if sd <= 0:
            return

        # 第 1 口止盈
        if not t.tp1_done:
            hit = (t.side == "long"  and c >= t.tp1_price) or \
                  (t.side == "short" and c <= t.tp1_price)
            if hit:
                t.tp1_done      = True
                t.contracts_left -= 1
                t.sl_price      = t.entry_price  # 移至成本
                t.trailing_sl   = t.entry_price
                t.trailing_active = True

                is_bt = getattr(self.trading_service, '_is_backtest', False)
                if not is_bt:
                    import asyncio
                    asyncio.get_event_loop().create_task(
                        self.trading_service.partial_close_position(
                            self.account_id, self.contract_id, 1 / t.n_contracts))

        # 追蹤止損（第 2、3 口）
        if t.trailing_active and t.contracts_left > 0:
            # 第 2 口用 fast，第 3 口用 slow
            trail_mult = self.trail_atr_fast if t.contracts_left == 1 \
                         else self.trail_atr_slow

            if t.side == "long":
                nsl = c - atr * trail_mult
                if nsl > t.trailing_sl and nsl > t.sl_price:
                    t.trailing_sl = nsl
                    t.sl_price    = nsl
                    self._sync_sl(t, nsl)
            else:
                nsl = c + atr * trail_mult
                if nsl < t.trailing_sl and nsl < t.sl_price:
                    t.trailing_sl = nsl
                    t.sl_price    = nsl
                    self._sync_sl(t, nsl)

    def _sync_sl(self, t: ActiveTrade, nsl: float) -> None:
        if abs(nsl - t.trailing_last) < self.TICK * 2:
            return
        t.trailing_last = nsl
        is_bt = getattr(self.trading_service, '_is_backtest', False)
        if not is_bt and t.order_id:
            import asyncio
            asyncio.get_event_loop().create_task(
                self.trading_service.modify_order(
                    self.account_id, t.order_id, stop_price=nsl))

    # ──────────────────────────────────────────────────────────────────────────
    # 下單
    # ──────────────────────────────────────────────────────────────────────────

    async def _place(self, sig: dict, atr: float) -> None:
        side   = sig["side"]
        entry  = sig["entry"]
        sl     = sig["sl"]
        tp1    = sig["tp1"]
        sd     = sig["sd"]
        n      = self.n_contracts

        try:
            oid = await self.trading_service.place_order(
                account_id  = self.account_id,
                contract_id = self.contract_id,
                order_type  = OrderType.MARKET,
                side        = OrderSide.BUY if side == "long" else OrderSide.SELL,
                size        = n,
                sl_ticks    = max(1, round(sd / self.TICK)),
                tp_ticks    = max(1, round(abs(tp1 - entry) / self.TICK)),
            )
        except Exception as e:
            logger.error(f"[v4] 下單失敗: {e}")
            return

        self.active_trade = ActiveTrade(
            side           = side,
            entry_price    = entry,
            sl_price       = sl,
            initial_sl     = sl,
            tp1_price      = tp1,
            n_contracts    = n,
            contracts_left = n,
            trailing_sl    = sl,
            order_id       = oid,
        )
        self._daily_trades += 1

        zone = sig["zone"]
        logger.info(
            f"[v4] {'多' if side=='long' else '空'} @ {entry:.2f} "
            f"SL={sl:.2f} TP1={tp1:.2f} RR={abs(tp1-entry)/sd:.1f} "
            f"SR={zone.mid:.1f}({zone.timeframe}) n={n}口"
        )

        if self.notifier and not getattr(self.trading_service, '_is_backtest', False):
            try:
                await self.notifier.send_message(
                    f"📈 *SNR v4*\n{'多' if side=='long' else '空'} @ {entry:.2f}\n"
                    f"SL={sl:.2f} TP={tp1:.2f} RR={abs(tp1-entry)/sd:.1f}R")
            except Exception:
                pass
