# strategies/snr_strategy_v3.py
"""
SNR 策略 v3 — 多時框順勢 × 小時框逆勢進場
==========================================
策略邏輯：
  1. 大時間框（1H / 4H）判斷趨勢方向（只做順勢方向）
  2. 密度法識別 15M / 1H / 4H / 1D 四個時間框的 SR 區
  3. 等待 15M 價格回踩至 SR 支撐區（多頭）/ 反彈至 SR 壓力區（空頭）
  4. 區內動能衰減（K棒實體縮小 or 成交量遞減）
  5. SR 邊緣假突破偵測（穿越深度 ≤ ATR×fake_break_depth_mult，收盤收回）
  6. 量增不破 + Pin Bar（可選但訊號更強）
  7. 收盤進場，止損設 Pin Bar 末端 / 假突破極值 + ATR×sl_buffer_mult
  8. 0.8R 部分止盈（整口平倉），1.2R 啟動移動止盈（透過 modify_order 更新 SL 子單）

與 v2 主要差異：
  - SR 識別：分型法 → 密度法（K棒實體密度）
  - 進場邏輯：掃止損同根收回 → SR區內假突破偵測
  - 趨勢過濾：4H+1H 強制全同向 → 大框定方向小框找逆勢機會
  - 移除：ADX 過濾、5M 精確化確認、SR 冷卻期（可選）
  - 新增：動能衰減偵測、多時框 SR 合併、假突破深度上限

系統對接（main.py）：
  from strategies.snr_strategy_v3 import SNRStrategyV3
  strategy = SNRStrategyV3(
      trading_service=self.trading_service,
      market_service=self.market_service,
      notifier=self.notifier,
      engine=self,
      contract_id=self.config["symbol"],
  )
  strategy.account_id = self.config["account_id"]   # ← 必填
  self.event_engine.register("ON_BAR", strategy.on_bar)

移動止損架構（bracket order）：
  TopstepX 的 bracket order 進場後，SL 和 TP 是獨立的子單掛在交易所。
  Python 端無法感知子單何時被打到（沒有 WebSocket 回調），
  因此用兩種方式確保狀態同步：
    1. 每根 K 棒檢查 DataHub.positions，若持倉消失代表 SL/TP 已成交
    2. 透過 searchOpen 找到 SL 子單 ID，用 modify_order 移動止損
"""

import asyncio
import talib
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from utils.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SRZone:
    top: float
    bottom: float
    strength: float         # 密度分數（越高越強）
    timeframe: str
    touch_count: int = 0    # 歷史觸碰次數

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.top - self.bottom


@dataclass
class FakeBreakout:
    """假突破訊號"""
    side: str               # "bullish"（做多）| "bearish"（做空）
    bar_idx: int            # 假突破發生的 bar index
    pierce_price: float     # 穿越到最遠的價格（做多=最低，做空=最高）
    close_price: float      # 收盤價（收回 SR 區內）
    volume: float           # 成交量
    avg_volume: float       # 近期均量
    is_pin_bar: bool = False
    atr: float = 0.0


@dataclass
class MomentumState:
    """動能衰減狀態"""
    decaying: bool = False
    bars_count: int = 0     # 連續衰減根數
    last_body: float = 0.0
    last_volume: float = 0.0


@dataclass
class ActiveTrade:
    side: str               # "long" | "short"
    entry_price: float
    sl_price: float
    tp_price: float
    atr: float
    risk_r: float           # 1R = 多少點

    # ── 訂單 ID 追蹤 ──────────────────────────────────────────────────────────
    # TopstepX bracket order 架構：
    #   place_order() → 回傳主單 order_id
    #   主單成交後，交易所自動掛 SL 子單 + TP 子單（各自有獨立 order_id）
    #   需透過 searchOpen 找到子單 ID，才能用 modify_order 移動止損
    entry_order_id: Optional[int] = None    # 主單 ID（市價單，成交後消失）
    sl_order_id: Optional[int] = None       # SL bracket 子單 ID（成交前一直掛著）
    tp_order_id: Optional[int] = None       # TP bracket 子單 ID

    # ── 出場管理狀態 ──────────────────────────────────────────────────────────
    trailing_active: bool = False
    trailing_stop: float = 0.0              # 目前移動止損的目標價格
    trailing_last_moved: float = 0.0        # 上次實際 modify_order 的價格（避免重複改單）
    partial_closed: bool = False
    is_closed: bool = False                 # 已平倉標記（防止重複操作）


# ──────────────────────────────────────────────────────────────────────────────
# 主策略
# ──────────────────────────────────────────────────────────────────────────────

class SNRStrategyV3(BaseStrategy):
    """
    多時框順勢 × 小時框逆勢進場策略 v3

    回測常調整參數（詳見 BACKTEST_PARAMS）：
      SR 識別：sr_lookback_* / sr_zone_atr_mult / sr_sensitivity / sr_body_weight
      進場條件：fake_break_depth_mult / vol_mult / momentum_bars / pin_bar_ratio
      風控：min_rr / sl_buffer_mult / sl_max_atr_mult / partial_tp_r / trailing_r
      趨勢：htf_structure_lookback / htf_primary（主趨勢框架）
    """

    # ── 回測參數對照表 ─────────────────────────────────────────────────────────
    BACKTEST_PARAMS = {
        # ── SR 密度法識別 ──────────────────────────────────
        "sr_lookback_15m":       500,    # 15M 回望根數（約1週）
        "sr_lookback_1h":        1000,   # 1H  回望根數（約6週）
        "sr_lookback_4h":        540,    # 4H  回望根數（約3個月）
        "sr_lookback_1d":        130,    # 1D  回望根數（約半年）
        "sr_zone_atr_mult":      0.10,   # 格寬 = ATR × 此值（越小越精細）
        "sr_sensitivity":        1.5,    # 門檻 = 均密度 × 此值（越小SR越多）
        "sr_body_weight":        0.7,    # K棒實體佔密度權重（0=全用影線,1=全用實體）
        "sr_max_zones":          12,     # 每個時框最多保留幾個 SR 區
        "sr_merge_dist_mult":    0.5,    # 跨時框合併距離 = ATR × 此值

        # ── 趨勢確認（大時間框）─────────────────────────────
        "htf_primary":           "4h",   # 主趨勢時框（"4h" or "1h"）
        "htf_secondary":         "1h",   # 次趨勢時框（兩者一致才進場）
        "htf_structure_lookback": 80,    # HTF 結構判斷回望根數
        "htf_swing_len":         3,      # 高低點判斷左右各幾根

        # ── 進場條件 ───────────────────────────────────────
        "sr_near_mult":          2.5,    # 「靠近SR區」= 距SR邊緣 ≤ ATR × 此值
        "fake_break_depth_mult": 0.20,   # 假突破最大深度 = ATR × 此值（超過=真突破）
        "vol_mult":              1.5,    # 量增門檻 = 近均量 × 此值
        "vol_lookback":          20,     # 均量計算根數
        "momentum_bars":         2,      # 動能衰減需連續幾根
        "pin_bar_ratio":         2.0,    # 影線 ≥ 實體 × 此值才算 Pin Bar（0=停用）
        "require_pin_bar":       False,  # 是否強制要求 Pin Bar

        # ── 風控 ───────────────────────────────────────────
        "min_rr":                1.5,    # 最低 RR 門檻
        "sl_buffer_mult":        0.15,   # 止損緩衝 = ATR × 此值
        "sl_max_atr_mult":       0.50,   # 止損距離上限 = ATR × 此值（超過跳過）
        "partial_tp_r":          0.80,   # 第一次部分止盈觸發（0.8R）
        "partial_close_ratio":   0.50,   # 部分止盈比例（50%）
        "trailing_r":            1.20,   # 移動止盈啟動（1.2R）

        # ── 時段過濾（UTC）─────────────────────────────────
        "session_london_start":  7,
        "session_london_end":    12,
        "session_ny_start":      13,
        "session_ny_start_min":  30,     # 13:30
        "session_ny_end":        20,
    }

    def __init__(self, trading_service, market_service, notifier, engine,
                 # ── SR 密度法識別
                 sr_lookback_15m: int = 500,
                 sr_lookback_1h: int = 1000,
                 sr_lookback_4h: int = 540,
                 sr_lookback_1d: int = 130,
                 sr_zone_atr_mult: float = 0.10,
                 sr_sensitivity: float = 1.5,
                 sr_body_weight: float = 0.70,
                 sr_max_zones: int = 12,
                 sr_merge_dist_mult: float = 0.50,
                 # ── 趨勢確認
                 htf_primary: str = "4h",
                 htf_secondary: str = "1h",
                 htf_structure_lookback: int = 80,
                 htf_swing_len: int = 3,
                 # ── 進場條件
                 sr_near_mult: float = 2.5,
                 fake_break_depth_mult: float = 0.20,
                 vol_mult: float = 1.5,
                 vol_lookback: int = 20,
                 momentum_bars: int = 2,
                 pin_bar_ratio: float = 2.0,
                 require_pin_bar: bool = False,
                 # ── 風控
                 min_rr: float = 1.5,
                 sl_buffer_mult: float = 0.15,
                 sl_max_atr_mult: float = 0.50,
                 partial_tp_r: float = 0.80,
                 partial_close_ratio: float = 0.50,
                 trailing_r: float = 1.20,
                 # ── 時段
                 session_london_start: int = 7,
                 session_london_end: int = 12,
                 session_ny_start: int = 13,
                 session_ny_start_min: int = 30,
                 session_ny_end: int = 20,
                 # ── 其他
                 atr_period: int = 14,
                 contract_id: str = "CON.F.US.MNQ.H26",
                 **kwargs):

        super().__init__(trading_service, market_service)
        # notifier 和 engine 由 v3 自行保存（BaseStrategy 只接受前兩個參數）
        self.notifier = notifier
        self.engine   = engine
        self.contract_id = contract_id

        # SR
        self.sr_lookback = {
            "15m": sr_lookback_15m,
            "1h":  sr_lookback_1h,
            "4h":  sr_lookback_4h,
            "1d":  sr_lookback_1d,
        }
        self.sr_zone_atr_mult  = sr_zone_atr_mult
        self.sr_sensitivity    = sr_sensitivity
        self.sr_body_weight    = sr_body_weight
        self.sr_max_zones      = sr_max_zones
        self.sr_merge_dist_mult = sr_merge_dist_mult

        # 趨勢
        self.htf_primary   = htf_primary
        self.htf_secondary = htf_secondary
        self.htf_structure_lookback = htf_structure_lookback
        self.htf_swing_len = htf_swing_len

        # 進場
        self.sr_near_mult          = sr_near_mult
        self.fake_break_depth_mult = fake_break_depth_mult
        self.vol_mult              = vol_mult
        self.vol_lookback          = vol_lookback
        self.momentum_bars         = momentum_bars
        self.pin_bar_ratio         = pin_bar_ratio
        self.require_pin_bar       = require_pin_bar

        # 風控
        self.min_rr             = min_rr
        self.sl_buffer_mult     = sl_buffer_mult
        self.sl_max_atr_mult    = sl_max_atr_mult
        self.partial_tp_r       = partial_tp_r
        self.partial_close_ratio = partial_close_ratio
        self.trailing_r         = trailing_r

        # 時段
        self.session_london_start  = session_london_start
        self.session_london_end    = session_london_end
        self.session_ny_start      = session_ny_start
        self.session_ny_start_min  = session_ny_start_min
        self.session_ny_end        = session_ny_end

        self.atr_period = atr_period

        # ── 系統對接 ──────────────────────────────────────────────────────────
        self.account_id: int = 0            # 由 main.py 注入：strategy.account_id = config["account_id"]
        self.TICK = 0.25                    # MNQ 最小跳動 0.25 點
        self.TICKS_PER_POINT = 4            # 1 點 = 4 ticks

        # 狀態
        self.active_trade: Optional[ActiveTrade] = None
        self._momentum: MomentumState = MomentumState()
        self._cached_sr_zones: List[SRZone] = []
        self._sr_cache_bar: int = -1          # 上次更新 SR 的 bar 序號
        self._sr_update_interval: int = 4     # 每隔幾根 15M K 棒重算一次 SR
        self._bar_count: int = 0

        # SL 子單查找：進場後最多等幾秒才去 searchOpen 找子單 ID
        self._sl_lookup_attempts: int = 0
        self._sl_lookup_max: int = 5         # 最多查 5 次（每根 K 棒查一次）

        # ── Dashboard 資料字典（供 dashboard.py 讀取，與 v2 格式相容）────────
        self.dashboard: dict = {
            "atr": 0.0,
            "sr_zones": [],
            "trend_4h": "─",
            "trend_1h": "─",
            "trend_15m": "─",
            "adx": 0.0,
            "session_open": False,
            "event_paused": False,
            "next_event": None,
            "status": "初始化中...",
            "last_update": None,
            "active_trade": None,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 主流程
    # ──────────────────────────────────────────────────────────────────────────

    async def on_bar(self, symbol: str, df: pd.DataFrame):
        """每根 15M K 棒收盤後呼叫"""
        if symbol != self.contract_id or df.empty or len(df) < 50:
            return

        self._bar_count += 1

        # 環境更新（不受時段限制，讓 dashboard 隨時顯示趨勢和 SR）
        await self._update_environment(df)

        # 1. 時段過濾
        if not self._is_session_open():
            self.dashboard["status"] = "休市時段"
            return

        # 2. 管理持倉（先於進場邏輯）
        if self.active_trade:
            await self._manage_trade(df)
            return

        # 3. 大時間框趨勢確認
        trend = self._get_htf_trend()
        if trend == "neutral":
            logger.debug("[SNRv3] HTF 趨勢中立，跳過")
            return

        # 4. 定期重算多時框 SR 區
        if self._bar_count - self._sr_cache_bar >= self._sr_update_interval:
            self._cached_sr_zones = self._build_mtf_sr_zones(df)
            self._sr_cache_bar = self._bar_count
            logger.debug(f"[SNRv3] SR 更新完成，共 {len(self._cached_sr_zones)} 個區域")

        if not self._cached_sr_zones:
            return

        # 5. 找進場
        await self._find_entry(df, trend)

    # ──────────────────────────────────────────────────────────────────────────
    # 時段過濾
    # ──────────────────────────────────────────────────────────────────────────

    def _is_session_open(self) -> bool:
        now = datetime.now(timezone.utc)
        h, m = now.hour, now.minute
        in_london = self.session_london_start <= h < self.session_london_end
        ny_start_ok = (h == self.session_ny_start and m >= self.session_ny_start_min) or \
                      (h > self.session_ny_start)
        in_ny = ny_start_ok and h < self.session_ny_end
        return in_london or in_ny

    # ──────────────────────────────────────────────────────────────────────────
    # Dashboard 環境更新（不受時段限制，讓儀表板隨時顯示最新狀態）
    # ──────────────────────────────────────────────────────────────────────────

    async def _update_environment(self, df: pd.DataFrame):
        """
        由 on_bar 和 dashboard._refresh_environment 呼叫。
        計算 ATR / SR / 多時框趨勢，寫入 self.dashboard，
        格式與 v2 相容，dashboard.py 不需修改。
        """
        try:
            from datetime import datetime
            atr = self._calc_atr(df)
            self.dashboard["atr"] = round(atr, 2)
            self.dashboard["session_open"] = self._is_session_open()
            self.dashboard["last_update"] = datetime.utcnow()

            # ── 多時框 SR（只在 atr > 0 時才算）────────────────────────────
            if atr > 0 and (self._bar_count - self._sr_cache_bar >= self._sr_update_interval
                            or not self._cached_sr_zones):
                self._cached_sr_zones = self._build_mtf_sr_zones(df)
                self._sr_cache_bar = self._bar_count

            sr_display = []
            for z in self._cached_sr_zones[:8]:
                sr_display.append({
                    "top":      z.top,
                    "bottom":   z.bottom,
                    "strength": round(z.strength, 3),
                    "tf":       z.timeframe,
                    "cooldown": 0,   # v3 無冷卻期
                })
            self.dashboard["sr_zones"] = sr_display

            # ── 多時框趨勢 ─────────────────────────────────────────────────
            trend_4h = self._htf_direction("4h")
            trend_1h = self._htf_direction("1h")

            # 15M 趨勢：用 v3 自身的上升/下降結構判斷
            t15_bull = self._is_uptrend(df)
            t15_bear = self._is_downtrend(df)
            t15_str  = "🟢多" if t15_bull else ("🔴空" if t15_bear else "⚪中性")

            label = {"bull": "🟢多", "bear": "🔴空", "neutral": "⚪中性"}
            self.dashboard["trend_4h"]  = label[trend_4h]
            self.dashboard["trend_1h"]  = label[trend_1h]
            self.dashboard["trend_15m"] = t15_str

            # ── ADX（用簡單 DX 均值近似）──────────────────────────────────
            try:
                import talib, numpy as np
                adx_vals = talib.ADX(
                    df['h'].values, df['l'].values, df['c'].values,
                    timeperiod=self.atr_period
                )
                self.dashboard["adx"] = round(float(adx_vals[-1]), 1) \
                    if not np.isnan(adx_vals[-1]) else 0.0
            except Exception:
                self.dashboard["adx"] = 0.0

            # ── 持倉狀態同步到 dashboard ───────────────────────────────────
            self.dashboard["active_trade"] = self.active_trade
            if self.active_trade and not self.active_trade.is_closed:
                self.dashboard["status"] = f"持倉中 {'多' if self.active_trade.side == 'long' else '空'}"
            elif not self._is_session_open():
                self.dashboard["status"] = "休市時段"
            else:
                self.dashboard["status"] = "等待訊號"

        except Exception as e:
            logger.debug(f"[SNRv3] _update_environment 異常: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # 時段過濾
    # ──────────────────────────────────────────────────────────────────────────


    def _get_htf_trend(self) -> str:
        """
        回傳 'bull' | 'bear' | 'neutral'
        主框架 + 次框架必須一致才確認趨勢
        """
        primary   = self._htf_direction(self.htf_primary)
        secondary = self._htf_direction(self.htf_secondary)

        if primary == secondary and primary != "neutral":
            return primary
        return "neutral"

    def _htf_direction(self, timeframe: str) -> str:
        """判斷指定時框的趨勢方向"""
        try:
            bs = getattr(self.engine, 'bar_store', None)
            if bs is None:
                return "neutral"
            df = bs.load(self.contract_id, timeframe, limit=self.htf_structure_lookback)
            if df.empty or len(df) < self.htf_swing_len * 4:
                return "neutral"
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_convert(None)

            bull = self._is_uptrend(df)
            bear = self._is_downtrend(df)
            if bull and not bear:
                return "bull"
            elif bear and not bull:
                return "bear"
            return "neutral"
        except Exception as e:
            logger.debug(f"[SNRv3] HTF direction error ({timeframe}): {e}")
            return "neutral"

    def _is_uptrend(self, df: pd.DataFrame) -> bool:
        """Higher High + Higher Low 結構"""
        highs, lows = df['h'].values, df['l'].values
        sl = self.htf_swing_len
        local_highs, local_lows = [], []

        for i in range(sl, len(df) - sl):
            if all(highs[i] >= highs[i - j] for j in range(1, sl + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, sl + 1)):
                local_highs.append(highs[i])
            if all(lows[i] <= lows[i - j] for j in range(1, sl + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, sl + 1)):
                local_lows.append(lows[i])

        hh = len(local_highs) >= 2 and local_highs[-1] > local_highs[-2]
        hl = len(local_lows) >= 2 and local_lows[-1] > local_lows[-2]
        return hh and hl

    def _is_downtrend(self, df: pd.DataFrame) -> bool:
        """Lower Low + Lower High 結構"""
        highs, lows = df['h'].values, df['l'].values
        sl = self.htf_swing_len
        local_highs, local_lows = [], []

        for i in range(sl, len(df) - sl):
            if all(highs[i] >= highs[i - j] for j in range(1, sl + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, sl + 1)):
                local_highs.append(highs[i])
            if all(lows[i] <= lows[i - j] for j in range(1, sl + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, sl + 1)):
                local_lows.append(lows[i])

        ll = len(local_lows) >= 2 and local_lows[-1] < local_lows[-2]
        lh = len(local_highs) >= 2 and local_highs[-1] < local_highs[-2]
        return ll and lh

    # ──────────────────────────────────────────────────────────────────────────
    # SR 密度法 — 核心
    # ──────────────────────────────────────────────────────────────────────────

    def _calc_sr_zones_density(self, df: pd.DataFrame, timeframe: str) -> List[SRZone]:
        """
        密度法識別 SR 區
        - 用 K 棒實體範圍做主要密度，影線做次要密度
        - 格寬 = ATR × sr_zone_atr_mult
        - 門檻 = 均密度 × sr_sensitivity
        """
        lookback = self.sr_lookback.get(timeframe, 500)
        if len(df) < max(lookback // 4, 20):
            return []

        window = df.tail(lookback).copy()
        atr    = self._calc_atr(df)
        if atr <= 0:
            return []

        zone_thickness = max(atr * self.sr_zone_atr_mult, 0.25)

        opens   = window['o'].values if 'o' in window.columns else window['c'].values
        highs   = window['h'].values
        lows    = window['l'].values
        closes  = window['c'].values
        vols    = window['v'].values if 'v' in window.columns else np.ones(len(window))
        N       = len(window)
        avg_vol = np.mean(vols) if np.mean(vols) > 0 else 1.0

        price_min = float(np.min(lows))
        price_max = float(np.max(highs))
        n_bins = max(int((price_max - price_min) / zone_thickness), 1)
        bin_density = np.zeros(n_bins)

        bw = self.sr_body_weight   # 實體權重
        sw = 1.0 - bw              # 影線權重

        for i in range(N):
            o_i, h_i, l_i, c_i = opens[i], highs[i], lows[i], closes[i]
            vol_factor = 1.0 + (vols[i] / avg_vol - 1.0) * 0.3  # 量大的K棒權重微增

            body_lo = min(o_i, c_i)
            body_hi = max(o_i, c_i)

            # 實體佔據的格子
            if body_hi > body_lo:
                b0 = max(int((body_lo - price_min) / zone_thickness), 0)
                b1 = min(int((body_hi - price_min) / zone_thickness), n_bins - 1)
                for b in range(b0, b1 + 1):
                    bin_density[b] += bw * vol_factor

            # 上影線
            if h_i > body_hi:
                b0 = max(int((body_hi - price_min) / zone_thickness), 0)
                b1 = min(int((h_i - price_min) / zone_thickness), n_bins - 1)
                for b in range(b0, b1 + 1):
                    bin_density[b] += sw * vol_factor * 0.5

            # 下影線
            if l_i < body_lo:
                b0 = max(int((l_i - price_min) / zone_thickness), 0)
                b1 = min(int((body_lo - price_min) / zone_thickness), n_bins - 1)
                for b in range(b0, b1 + 1):
                    bin_density[b] += sw * vol_factor * 0.5

        # 計算門檻
        nonzero = bin_density[bin_density > 0]
        if len(nonzero) == 0:
            return []
        avg_density = np.mean(nonzero)
        threshold   = avg_density * self.sr_sensitivity

        zones: List[SRZone] = []
        for i, density in enumerate(bin_density):
            if density > threshold:
                bottom = price_min + i * zone_thickness
                top    = bottom + zone_thickness
                zones.append(SRZone(
                    top=round(top, 2),
                    bottom=round(bottom, 2),
                    strength=float(density),
                    timeframe=timeframe,
                ))

        zones.sort(key=lambda z: z.strength, reverse=True)
        return zones[:self.sr_max_zones]

    def _build_mtf_sr_zones(self, df_15m: pd.DataFrame) -> List[SRZone]:
        """
        整合多時框 SR 區
        15M / 1H / 4H / 1D 都算，靠近的區域合併
        """
        atr = self._calc_atr(df_15m)
        merge_dist = atr * self.sr_merge_dist_mult

        all_zones: List[SRZone] = []

        # 15M（直接用傳入的 df）
        zones_15m = self._calc_sr_zones_density(df_15m, "15m")
        all_zones.extend(zones_15m)

        # 其他時框從 bar_store 讀取
        bs = getattr(self.engine, 'bar_store', None)
        for tf in ["1h", "4h", "1d"]:
            if bs is None:
                break
            try:
                limit = self.sr_lookback.get(tf, 500)
                df_tf = bs.load(self.contract_id, tf, limit=limit)
                if df_tf.empty:
                    continue
                if hasattr(df_tf.index, 'tz') and df_tf.index.tz is not None:
                    df_tf.index = df_tf.index.tz_convert(None)
                zones_tf = self._calc_sr_zones_density(df_tf, tf)
                all_zones.extend(zones_tf)
            except Exception as e:
                logger.debug(f"[SNRv3] MTF SR load error ({tf}): {e}")

        if not all_zones:
            return []

        # 合併：mid 距離 < merge_dist 的區域合併，強度加總
        all_zones.sort(key=lambda z: z.mid)
        merged: List[SRZone] = []
        current = all_zones[0]
        extra_strength = 0.0

        for z in all_zones[1:]:
            if abs(z.mid - current.mid) <= merge_dist:
                # 合併：取最大範圍，強度加總，記跨時框次數
                new_bottom = min(current.bottom, z.bottom)
                new_top    = max(current.top, z.top)
                # 多時框重疊增加權重
                tf_bonus = 1.5 if z.timeframe != current.timeframe else 1.0
                merged_strength = current.strength + z.strength * tf_bonus
                current = SRZone(
                    top=round(new_top, 2),
                    bottom=round(new_bottom, 2),
                    strength=merged_strength,
                    timeframe=f"{current.timeframe}+{z.timeframe}",
                    touch_count=current.touch_count + 1,
                )
            else:
                merged.append(current)
                current = z

        merged.append(current)
        merged.sort(key=lambda z: z.strength, reverse=True)
        return merged[:self.sr_max_zones]

    # ──────────────────────────────────────────────────────────────────────────
    # 動能衰減偵測
    # ──────────────────────────────────────────────────────────────────────────

    def _update_momentum(self, df: pd.DataFrame):
        """更新動能衰減狀態（在 SR 區附近時才監控）"""
        if len(df) < 3:
            return

        opens  = df['o'].values if 'o' in df.columns else df['c'].values
        closes = df['c'].values
        vols   = df['v'].values if 'v' in df.columns else np.ones(len(df))

        # 近 N 根
        N = self.momentum_bars + 2
        recent_bodies = [abs(closes[-(i+1)] - opens[-(i+1)]) for i in range(N)]
        recent_vols   = [vols[-(i+1)] for i in range(N)]

        # 連續實體縮小 or 成交量遞減
        body_shrinking = all(recent_bodies[i] < recent_bodies[i+1] for i in range(self.momentum_bars))
        vol_declining  = all(recent_vols[i] < recent_vols[i+1]   for i in range(self.momentum_bars))

        if body_shrinking or vol_declining:
            self._momentum.decaying  = True
            self._momentum.bars_count += 1
        else:
            self._momentum.decaying  = False
            self._momentum.bars_count = 0

    # ──────────────────────────────────────────────────────────────────────────
    # 假突破偵測
    # ──────────────────────────────────────────────────────────────────────────

    def _detect_fake_breakout(self, df: pd.DataFrame, zone: SRZone,
                               side: str, atr: float) -> Optional[FakeBreakout]:
        """
        偵測 SR 邊緣的假突破
        side: "bullish"（做多，等假突破支撐下緣後收回）
              "bearish"（做空，等假突破壓力上緣後收回）
        """
        if len(df) < 3:
            return None

        opens  = df['o'].values if 'o' in df.columns else df['c'].values
        highs  = df['h'].values
        lows   = df['l'].values
        closes = df['c'].values
        vols   = df['v'].values if 'v' in df.columns else np.ones(len(df))

        avg_vol = np.mean(vols[-self.vol_lookback:]) if len(vols) >= self.vol_lookback \
                  else np.mean(vols)

        max_depth = atr * self.fake_break_depth_mult

        # 只看最近 2 根（當根 + 前根）
        for i in [-2, -1]:
            idx = len(df) + i
            if idx < 0:
                continue

            if side == "bullish":
                # 最低點穿越 SR 下緣，但收盤在 SR 區內
                pierce_price = lows[idx]
                close_price  = closes[idx]
                sr_edge      = zone.bottom

                if pierce_price < sr_edge and \
                   (sr_edge - pierce_price) <= max_depth and \
                   close_price >= sr_edge and \
                   vols[idx] >= avg_vol * self.vol_mult:

                    is_pin = self._check_pin_bar(opens[idx], highs[idx], lows[idx], closes[idx], "bullish")
                    return FakeBreakout(
                        side="bullish",
                        bar_idx=idx,
                        pierce_price=pierce_price,
                        close_price=close_price,
                        volume=vols[idx],
                        avg_volume=avg_vol,
                        is_pin_bar=is_pin,
                        atr=atr,
                    )

            elif side == "bearish":
                # 最高點穿越 SR 上緣，但收盤在 SR 區內
                pierce_price = highs[idx]
                close_price  = closes[idx]
                sr_edge      = zone.top

                if pierce_price > sr_edge and \
                   (pierce_price - sr_edge) <= max_depth and \
                   close_price <= sr_edge and \
                   vols[idx] >= avg_vol * self.vol_mult:

                    is_pin = self._check_pin_bar(opens[idx], highs[idx], lows[idx], closes[idx], "bearish")
                    return FakeBreakout(
                        side="bearish",
                        bar_idx=idx,
                        pierce_price=pierce_price,
                        close_price=close_price,
                        volume=vols[idx],
                        avg_volume=avg_vol,
                        is_pin_bar=is_pin,
                        atr=atr,
                    )

        return None

    def _check_pin_bar(self, o: float, h: float, l: float, c: float, side: str) -> bool:
        """Pin Bar 判斷"""
        if self.pin_bar_ratio <= 0:
            return True  # 停用時視為通過
        body = abs(c - o)
        if body < 1e-9:
            body = (h - l) * 0.1

        if side == "bullish":
            lower_shadow = min(o, c) - l
            return lower_shadow >= body * self.pin_bar_ratio
        else:
            upper_shadow = h - max(o, c)
            return upper_shadow >= body * self.pin_bar_ratio

    # ──────────────────────────────────────────────────────────────────────────
    # 進場邏輯
    # ──────────────────────────────────────────────────────────────────────────

    async def _find_entry(self, df: pd.DataFrame, trend: str):
        """
        尋找進場機會
        trend = "bull" → 找 SR 支撐區的假突破做多
        trend = "bear" → 找 SR 壓力區的假突破做空
        """
        atr = self._calc_atr(df)
        if atr <= 0:
            return

        current_price = float(df['c'].iloc[-1])

        # 更新動能衰減狀態
        self._update_momentum(df)

        # 決定要找的方向
        entry_side = "bullish" if trend == "bull" else "bearish"

        # 找最近的 SR 區
        near_zone = self._find_near_zone(current_price, entry_side, atr)
        if near_zone is None:
            return

        # 動能衰減確認（需要連續 momentum_bars 根以上）
        if not self._momentum.decaying:
            logger.debug(f"[SNRv3] 動能未衰減，等待 SR {near_zone.mid:.1f}")
            return

        # 假突破偵測
        fb = self._detect_fake_breakout(df, near_zone, entry_side, atr)
        if fb is None:
            return

        # 是否強制要求 Pin Bar
        if self.require_pin_bar and not fb.is_pin_bar:
            logger.info(f"[SNRv3] 假突破出現但無 Pin Bar，跳過（require_pin_bar=True）")
            return

        # 計算止損止盈
        if not await self._evaluate_trade(df, fb, near_zone, atr, trend):
            return

    def _find_near_zone(self, price: float, side: str, atr: float) -> Optional[SRZone]:
        """
        找符合條件的 SR 區：
        - bullish：在 price 下方，且距離 ≤ sr_near_mult × ATR
        - bearish：在 price 上方，且距離 ≤ sr_near_mult × ATR
        """
        max_dist = atr * self.sr_near_mult
        candidates = []

        for z in self._cached_sr_zones:
            if side == "bullish":
                # price 在 SR 區內或剛進入 SR 區上方
                in_zone = z.bottom <= price <= z.top
                near_above = price > z.top and (price - z.top) <= max_dist
                if in_zone or near_above:
                    candidates.append((z, abs(price - z.mid)))
            elif side == "bearish":
                in_zone = z.bottom <= price <= z.top
                near_below = price < z.bottom and (z.bottom - price) <= max_dist
                if in_zone or near_below:
                    candidates.append((z, abs(price - z.mid)))

        if not candidates:
            return None

        # 最近且最強的（距離 × 1/強度 排序）
        candidates.sort(key=lambda x: x[1] / (x[0].strength + 1e-9))
        return candidates[0][0]

    async def _evaluate_trade(self, df: pd.DataFrame, fb: FakeBreakout,
                               zone: SRZone, atr: float, trend: str) -> bool:
        """計算 SL/TP，驗證 RR，通過後下單"""
        opens  = df['o'].values if 'o' in df.columns else df['c'].values
        closes = df['c'].values

        entry_price = float(closes[-1])
        buffer      = atr * self.sl_buffer_mult

        if fb.side == "bullish":
            # 止損：假突破最低點 - buffer
            sl_price = fb.pierce_price - buffer
            sl_dist  = entry_price - sl_price

            if sl_dist <= 0 or sl_dist > atr * self.sl_max_atr_mult:
                logger.info(f"[SNRv3] 止損距離 {sl_dist:.1f} 超出上限 {atr * self.sl_max_atr_mult:.1f}，跳過")
                return False

            # 止盈：最近上方 SR 壓力區下緣
            tp_zone = self._find_tp_zone(entry_price, "above")
            if tp_zone is None:
                logger.info("[SNRv3] 找不到對面 SR 區，跳過")
                return False
            tp_price = tp_zone.bottom

        else:
            # 止損：假突破最高點 + buffer
            sl_price = fb.pierce_price + buffer
            sl_dist  = sl_price - entry_price

            if sl_dist <= 0 or sl_dist > atr * self.sl_max_atr_mult:
                logger.info(f"[SNRv3] 止損距離 {sl_dist:.1f} 超出上限 {atr * self.sl_max_atr_mult:.1f}，跳過")
                return False

            # 止盈：最近下方 SR 支撐區上緣
            tp_zone = self._find_tp_zone(entry_price, "below")
            if tp_zone is None:
                logger.info("[SNRv3] 找不到對面 SR 區，跳過")
                return False
            tp_price = tp_zone.top

        # RR 檢查
        tp_dist = abs(tp_price - entry_price)
        rr      = tp_dist / sl_dist if sl_dist > 0 else 0.0

        if rr < self.min_rr:
            logger.info(f"[SNRv3] RR={rr:.2f} < 門檻 {self.min_rr}，跳過")
            return False

        pin_tag = "✓ Pin Bar" if fb.is_pin_bar else "無 Pin Bar"
        logger.info(
            f"[SNRv3] 進場 {'做多' if fb.side == 'bullish' else '做空'} "
            f"| 進場={entry_price:.1f} SL={sl_price:.1f} TP={tp_price:.1f} "
            f"RR={rr:.2f} | {pin_tag} | SR={zone.mid:.1f}({zone.timeframe})"
        )

        sl_ticks = round(sl_dist)
        tp_ticks = round(tp_dist)
        await self._place_order(
            side=OrderSide.BUY if fb.side == "bullish" else OrderSide.SELL,
            sl_ticks=sl_ticks,
            tp_ticks=tp_ticks,
            sl_price=sl_price,
            tp_price=tp_price,
            atr=atr,
            entry_price=entry_price,
            risk_r=sl_dist,
        )
        return True

    def _find_tp_zone(self, price: float, direction: str) -> Optional[SRZone]:
        """找最近的對面 SR 區（止盈目標）"""
        candidates = []
        for z in self._cached_sr_zones:
            if direction == "above" and z.bottom > price:
                candidates.append(z)
            elif direction == "below" and z.top < price:
                candidates.append(z)
        if not candidates:
            return None
        if direction == "above":
            return min(candidates, key=lambda z: z.bottom)
        else:
            return max(candidates, key=lambda z: z.top)

    # ──────────────────────────────────────────────────────────────────────────
    # 持倉管理
    # ──────────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────────
    # 持倉管理
    # ──────────────────────────────────────────────────────────────────────────

    async def _manage_trade(self, df: pd.DataFrame):
        """
        每根 15M K 棒收盤後呼叫。
        流程：
          1. 確認持倉還在（防止 SL/TP 已被交易所執行但 Python 端未知）
          2. 若 sl_order_id 尚未找到 → 從 searchOpen 查找 bracket 子單
          3. 判斷是否觸發部分止盈（0.8R → 平半倉，同時把 SL 移到 BE）
          4. 判斷是否啟動移動止盈（1.2R → trailing_active = True）
          5. 移動止盈邏輯：計算新 SL 目標，若移動量足夠 → modify_order 更新 SL 子單
        """
        trade = self.active_trade
        if not trade or trade.is_closed:
            return

        # ── Step 1：確認持倉還在 ──────────────────────────────────────────────
        # DataHub.positions 由 OrderMonitor 每 2 秒更新
        from core.data_hub import DataHub
        pos = DataHub.positions.get(self.contract_id)
        if pos is None:
            # 持倉消失 → SL 或 TP 已被交易所執行，清除本地狀態
            logger.info("[SNRv3] 持倉消失（SL/TP 已成交），清除本地紀錄")
            trade.is_closed = True
            self.active_trade = None
            self._sl_lookup_attempts = 0
            return

        current_price = float(df['c'].iloc[-1])
        r = trade.risk_r  # 1R = 多少點

        # ── Step 2：查找 SL bracket 子單 ID ──────────────────────────────────
        # bracket 子單在主單成交後才會出現在 searchOpen，需等一段時間再查
        if trade.sl_order_id is None and self._sl_lookup_attempts < self._sl_lookup_max:
            await self._find_bracket_order_ids(trade)
            self._sl_lookup_attempts += 1

        # ── Step 3 & 4 & 5：依方向處理 ───────────────────────────────────────
        if trade.side == "long":
            profit_r = (current_price - trade.entry_price) / r if r > 0 else 0.0

            # 部分止盈（0.8R）
            if not trade.partial_closed and profit_r >= self.partial_tp_r:
                await self._do_partial_close(trade, current_price)

            # 移動止盈啟動（1.2R）
            if not trade.trailing_active and profit_r >= self.trailing_r:
                trade.trailing_active = True
                trade.trailing_stop   = trade.entry_price   # 初始設在 BE（損益平衡）
                logger.info(
                    f"[SNRv3] 啟動移動止盈（{profit_r:.2f}R）"
                    f"SL 移至 BE={trade.entry_price:.2f}"
                )
                await self._move_sl(trade, trade.entry_price)

            # 持續更新移動止盈
            if trade.trailing_active:
                # 新 SL 目標 = 當前價格 - 0.5 ATR（保持一定緩衝）
                new_trail = current_price - trade.atr * 0.5
                if new_trail > trade.trailing_stop:
                    trade.trailing_stop = new_trail
                    await self._move_sl(trade, new_trail)

        elif trade.side == "short":
            profit_r = (trade.entry_price - current_price) / r if r > 0 else 0.0

            if not trade.partial_closed and profit_r >= self.partial_tp_r:
                await self._do_partial_close(trade, current_price)

            if not trade.trailing_active and profit_r >= self.trailing_r:
                trade.trailing_active = True
                trade.trailing_stop   = trade.entry_price
                logger.info(
                    f"[SNRv3] 啟動移動止盈（{profit_r:.2f}R）"
                    f"SL 移至 BE={trade.entry_price:.2f}"
                )
                await self._move_sl(trade, trade.entry_price)

            if trade.trailing_active:
                new_trail = current_price + trade.atr * 0.5
                if new_trail < trade.trailing_stop:
                    trade.trailing_stop = new_trail
                    await self._move_sl(trade, new_trail)

    async def _find_bracket_order_ids(self, trade: ActiveTrade):
        """
        進場後從 searchOpen 找到 SL/TP bracket 子單的 order_id。

        TopstepX bracket 架構：
          主單（市價單）成交 → 自動產生 SL 子單（stop order）和 TP 子單（limit order）
          子單會出現在 searchOpen，可以用 side 和 stopPrice/limitPrice 來辨識。

          SL 子單辨識：
            - side 與主單相反（做多的 SL 是賣單）
            - stopPrice ≈ trade.sl_price
          TP 子單辨識：
            - side 與主單相反
            - limitPrice ≈ trade.tp_price
        """
        try:
            res = await self.trading_service.client.request(
                "POST", "/api/Order/searchOpen",
                json={"accountId": self.account_id}
            )
            if not res or not res.get("success"):
                return

            orders = res.get("orders", [])
            expected_side = 1 if trade.side == "long" else 0   # SL/TP 方向與進場相反

            for o in orders:
                if o.get("contractId") != self.contract_id:
                    continue
                if o.get("side") != expected_side:
                    continue

                stop_p  = o.get("stopPrice")
                limit_p = o.get("limitPrice")
                oid     = o.get("id")

                # 辨識 SL 子單（stop price 接近我們設的 sl_price，允許 2 點誤差）
                if stop_p and abs(stop_p - trade.sl_price) < 2.0 and trade.sl_order_id is None:
                    trade.sl_order_id = oid
                    logger.info(f"[SNRv3] 找到 SL 子單 #{oid} stop={stop_p:.2f}")

                # 辨識 TP 子單（limit price 接近 tp_price）
                if limit_p and abs(limit_p - trade.tp_price) < 2.0 and trade.tp_order_id is None:
                    trade.tp_order_id = oid
                    logger.info(f"[SNRv3] 找到 TP 子單 #{oid} limit={limit_p:.2f}")

        except Exception as e:
            logger.warning(f"[SNRv3] 查找 bracket 子單失敗: {e}")

    async def _move_sl(self, trade: ActiveTrade, new_sl_price: float):
        """
        透過 modify_order 把交易所掛著的 SL 子單移到新價格。

        注意：
          - 只有在移動量 > 1 點才實際發 API（避免頻繁改單）
          - 做多：新 SL 只能往上移（保護獲利），不能往下移
          - 做空：新 SL 只能往下移，不能往上移
          - sl_order_id 必須已找到，否則跳過（等下一根 K 棒再試）
        """
        if trade.sl_order_id is None:
            logger.debug("[SNRv3] 尚未找到 SL 子單 ID，移動止損暫緩")
            return

        # 四捨五入到最近 tick
        new_sl_price = round(round(new_sl_price / self.TICK) * self.TICK, 2)

        # 方向保護：只往有利方向移
        if trade.side == "long" and new_sl_price <= trade.trailing_last_moved:
            return
        if trade.side == "short" and new_sl_price >= trade.trailing_last_moved:
            return

        # 移動量過小（< 1 點）不發 API，減少請求次數
        if abs(new_sl_price - trade.trailing_last_moved) < 1.0:
            return

        try:
            ok = await self.trading_service.modify_order(
                account_id=self.account_id,
                order_id=trade.sl_order_id,
                stop_price=new_sl_price,
            )
            if ok:
                logger.info(
                    f"[SNRv3] 移動止損成功 "
                    f"{'↑' if trade.side == 'long' else '↓'} "
                    f"{trade.trailing_last_moved:.2f} → {new_sl_price:.2f}"
                )
                trade.trailing_last_moved = new_sl_price
                trade.sl_price = new_sl_price   # 同步本地記錄
            else:
                logger.warning(f"[SNRv3] modify_order 失敗，下一根 K 棒重試")
        except Exception as e:
            logger.warning(f"[SNRv3] 移動止損異常: {e}")

    async def _do_partial_close(self, trade: ActiveTrade, current_price: float):
        """
        部分止盈：平掉 50% 倉位（1口時 = 整口平，但策略只開1口）
        
        注意：MNQ 固定 1 口，部分止盈等同整口平倉後重新管理。
        這裡的做法是：
          1. 整口平倉（close_position）
          2. 取消剩餘的 SL / TP 子單（平倉後子單會自動取消，但確認一次）
          3. 清除 active_trade，後續不再管理
        
        若未來改為 2 口以上，可改用 partial_close_position(size=1)，
        並保留剩餘倉位繼續移動止損。
        """
        try:
            profit_pts = abs(current_price - trade.entry_price)
            logger.info(
                f"[SNRv3] 部分止盈觸發 @ {current_price:.2f} "
                f"（+{profit_pts:.1f} 點 / +{profit_pts * 2:.0f} USD）"
            )

            # 目前固定 1 口 → 整口平倉
            ok = await self.trading_service.close_position(
                account_id=self.account_id,
                contract_id=self.contract_id,
            )

            if ok:
                trade.partial_closed = True
                trade.is_closed = True
                self.active_trade = None
                self._sl_lookup_attempts = 0
                logger.info("[SNRv3] 部分止盈平倉完成，清除持倉狀態")
                if self.notifier:
                    await self.notifier.send_message(
                        f"✅ *[SNR v3] 部分止盈*\n"
                        f"方向：{'做多' if trade.side == 'long' else '做空'}\n"
                        f"進場：`{trade.entry_price:.2f}`\n"
                        f"平倉：`{current_price:.2f}`\n"
                        f"損益：`+{profit_pts * 2:.0f} USD`"
                    )
            else:
                logger.warning("[SNRv3] 部分止盈平倉失敗，下一根 K 棒重試")

        except Exception as e:
            logger.warning(f"[SNRv3] 部分止盈異常: {e}")

    async def _close_trade(self, trade: ActiveTrade, reason: str = ""):
        """
        主動平倉（策略觸發，如移動止損邏輯判斷需平倉）
        注意：SL/TP 被交易所直接打到時不會經過這裡，
        而是由 _manage_trade 的 DataHub 持倉檢查偵測到。
        """
        if trade.is_closed:
            return
        try:
            ok = await self.trading_service.close_position(
                account_id=self.account_id,
                contract_id=self.contract_id,
            )
            if ok:
                trade.is_closed = True
                self.active_trade = None
                self._sl_lookup_attempts = 0
                logger.info(f"[SNRv3] 主動平倉完成，原因: {reason}")
            else:
                logger.warning(f"[SNRv3] 主動平倉失敗，原因: {reason}")
        except Exception as e:
            logger.warning(f"[SNRv3] 平倉異常: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # 下單
    # ──────────────────────────────────────────────────────────────────────────

    async def _place_order(self, side: OrderSide, sl_ticks: int, tp_ticks: int,
                            sl_price: float, tp_price: float, atr: float,
                            entry_price: float, risk_r: float):
        """
        下市價單 + bracket SL/TP。

        TradingService.place_order 參數說明：
          - account_id: 帳戶 ID（從 main.py 注入）
          - side: 0=BUY, 1=SELL（傳 int，不是 enum）
          - order_type: 1=MARKET
          - sl_ticks / tp_ticks: 正整數，系統內部自動轉正負號
            MNQ: 1 點 = 4 ticks，止損 8 點 = 32 ticks
        """
        if not self.account_id:
            logger.error("[SNRv3] account_id 未設定，無法下單（請在 main.py 設定 strategy.account_id）")
            return

        # 點數轉 ticks（取整數，最少 1 tick）
        sl_ticks_int = max(round(risk_r / self.TICK), 1)
        tp_ticks_int = max(round(abs(tp_price - entry_price) / self.TICK), 1)

        try:
            order_id = await self.trading_service.place_order(
                account_id  = self.account_id,
                contract_id = self.contract_id,
                order_type  = int(OrderType.MARKET),
                side        = int(side),
                size        = 1,
                sl_ticks    = sl_ticks_int,
                tp_ticks    = tp_ticks_int,
            )

            if order_id is None:
                logger.error("[SNRv3] 下單失敗，place_order 回傳 None")
                return

            trade_side = "long" if side == OrderSide.BUY else "short"
            self.active_trade = ActiveTrade(
                side            = trade_side,
                entry_price     = entry_price,
                sl_price        = sl_price,
                tp_price        = tp_price,
                atr             = atr,
                risk_r          = risk_r,
                entry_order_id  = order_id,
                trailing_last_moved = sl_price,   # 初始值 = 原始止損位
            )
            self._sl_lookup_attempts = 0   # 重置查找計數

            logger.info(
                f"[SNRv3] 下單成功 #{order_id} | {trade_side} @ {entry_price:.2f} "
                f"| SL={sl_price:.2f}（{sl_ticks_int}t）"
                f"| TP={tp_price:.2f}（{tp_ticks_int}t）"
                f"| RR={abs(tp_price-entry_price)/risk_r:.2f}"
            )

            if self.notifier:
                await self.notifier.send_message(
                    f"📈 *[SNR v3] 進場*\n"
                    f"方向：{'🟢 做多' if trade_side == 'long' else '🔴 做空'}\n"
                    f"進場：`{entry_price:.2f}`\n"
                    f"止損：`{sl_price:.2f}`（{risk_r:.1f} 點）\n"
                    f"止盈：`{tp_price:.2f}`\n"
                    f"RR：`{abs(tp_price-entry_price)/risk_r:.2f}R`"
                )

        except Exception as e:
            logger.error(f"[SNRv3] 下單異常: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame) -> float:
        if len(df) < self.atr_period + 1:
            return 0.0
        try:
            atr_vals = talib.ATR(
                df['h'].values, df['l'].values, df['c'].values,
                timeperiod=self.atr_period
            )
            val = float(atr_vals[-1])
            return val if not np.isnan(val) else 0.0
        except Exception:
            # fallback: 簡單 TR 均值
            tr = df['h'] - df['l']
            return float(tr.tail(self.atr_period).mean())

    def get_pos_size(self) -> int:
        return 1  # 固定 1 口
