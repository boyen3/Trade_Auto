# strategies/snr_strategy.py
"""
SNR 流動性掠奪策略 v2
======================
新增功能：
  1. 多時框趨勢（4H 背景不反向 + 1H 方向確認）
  2. ADX > 20 + 近10根範圍 > ATR×1.5 盤整過濾
  3. 5M 進場精確化（15M 訊號後等 5M 同向確認）
  4. 交易時段過濾（倫敦 07:00-12:00 + 紐約 13:30-20:00 UTC）
  5. SR 冷卻期（同一區被止損後冷卻 3 根 K 棒）
  6. 部分止盈（先平 50%，剩下移動止盈）
  7. Pin Bar 或 吞噬 型態確認
  8. 經濟事件過濾（ForexFactory，事件前15分暫停，後等一根15M確認）
"""

import asyncio
import httpx
import talib
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from strategies.base_strategy import BaseStrategy
from core.constants import OrderSide, OrderType
from core.data_hub import DataHub
from utils.logger import logger

# ──────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────

@dataclass
class SRZone:
    top: float
    bottom: float
    strength: float
    timeframe: str

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class LiquiditySweep:
    side: str           # "bullish" | "bearish"
    sweep_price: float
    sweep_bar_idx: int
    recover_bar_idx: int
    shadow_tip: float
    atr: float
    is_pin_bar: bool = False
    is_engulfing: bool = False


@dataclass
class ActiveTrade:
    side: str                   # "long" | "short"
    entry_price: float
    sl_price: float
    tp_price: float             # 原始止盈
    trailing_tp: float          # 移動止盈
    sl_ticks: int
    risk_amount: float
    trailing_active: bool = False
    partial_closed: bool = False    # 是否已部分平倉
    order_id: Optional[int] = None
    sr_zone_mid: float = 0.0        # 進場時的 SR 區中心（冷卻期用）


@dataclass
class EconomicEvent:
    title: str
    timestamp: datetime
    impact: str     # "High" | "Medium" | "Low"


# ──────────────────────────────────────────
# 主策略
# ──────────────────────────────────────────

class SNRStrategy(BaseStrategy):

    def __init__(self, trading_service, market_service, notifier, engine,
                 # SR 參數
                 sr_lookback_15m: int = 150,
                 sr_lookback_1h: int = 300,
                 sr_lookback_4h: int = 600,
                 sr_atr_mult: float = 0.25,
                 sr_side_len: int = 2,
                 sr_sensitivity: float = 1.5,   # G組: 放寬SR門檻
                 sr_vol_weight: float = 0.5,
                 sr_time_decay: float = 2.0,
                 # 流動性掠奪參數
                 sweep_atr_mult: float = 0.15,
                 sweep_vol_mult: float = 1.5,
                 sweep_lookback: int = 20,
                 # 盤整過濾
                 adx_period: int = 14,
                 adx_threshold: float = 15.0,   # G組: 放寬盤整門檻
                 range_atr_mult: float = 1.5,
                 range_lookback: int = 10,
                 # 5M 精確化
                 use_5m_confirm: bool = True,
                 # 時段過濾（UTC）
                 session_london_start: int = 7,
                 session_london_end: int = 12,
                 session_ny_start: int = 13,
                 session_ny_end: int = 20,
                 session_ny_start_min: int = 30,   # 13:30
                 # 型態確認
                 pin_bar_ratio: float = 0.0,        # 型態確認已停用（G組設定）
                 # 風控
                 atr_period: int = 14,
                 sl_buffer_ticks: int = 3,
                 min_rr: float = 1.5,
                 trailing_rr_trigger: float = 1.2,  # G組: 更早啟動移動止盈
                 partial_close_ratio: float = 0.5,  # 部分平倉比例
                 max_risk_pct: float = 0.01,
                 tick_size: float = 0.25,
                 size: int = 1,
                 # SR 冷卻
                 sr_cooldown_bars: int = 3,
                 # 事件過濾（分鐘）
                 event_pause_before: int = 15,
                 event_resume_after_bars: int = 1):

        super().__init__(trading_service, market_service)
        self.strategy_name = "SNR_v2"
        self.notifier      = notifier
        self.engine        = engine

        # SR
        self.sr_lookback    = {"15m": sr_lookback_15m, "1h": sr_lookback_1h, "4h": sr_lookback_4h}
        self.sr_atr_mult    = sr_atr_mult
        self.sr_side_len    = sr_side_len
        self.sr_sensitivity = sr_sensitivity
        self.sr_vol_weight  = sr_vol_weight
        self.sr_time_decay  = sr_time_decay

        # 掃止損
        self.sweep_atr_mult = sweep_atr_mult
        self.sweep_vol_mult = sweep_vol_mult
        self.sweep_lookback = sweep_lookback

        # 盤整過濾
        self.adx_period     = adx_period
        self.adx_threshold  = adx_threshold
        self.range_atr_mult = range_atr_mult
        self.range_lookback = range_lookback

        # 5M 確認
        self.use_5m_confirm = use_5m_confirm
        self._pending_5m_signal: Optional[dict] = None  # 等待 5M 確認的訊號

        # 時段
        self.session_london = (session_london_start, session_london_end)
        self.session_ny     = (session_ny_start + session_ny_start_min / 60, session_ny_end)

        # 型態
        self.pin_bar_ratio  = pin_bar_ratio

        # 風控
        self.atr_period          = atr_period
        self.sl_buffer_ticks     = sl_buffer_ticks
        self.min_rr              = min_rr
        self.trailing_rr_trigger = trailing_rr_trigger
        self.partial_close_ratio = partial_close_ratio
        self.max_risk_pct        = max_risk_pct
        self.tick_size           = tick_size
        self.size                = size

        # SR 冷卻
        self.sr_cooldown_bars = sr_cooldown_bars
        self._sr_cooldown: Dict[float, int] = {}    # {sr_mid: 剩餘冷卻 bar 數}

        # 事件過濾
        self.event_pause_before      = event_pause_before
        self.event_resume_after_bars = event_resume_after_bars
        self._events: List[EconomicEvent] = []
        self._events_loaded_date: Optional[str] = None
        self._event_paused          = False
        self._event_bars_after      = 0    # 事件後等待 bar 數計數
        self._last_event_notified   = None

        # 狀態
        self._is_processing   = False
        self._last_signal_bar = -1
        self._active_trade: Optional[ActiveTrade] = None
        self._sr_cache: List[SRZone] = []
        self._sr_last_bar   = -1
        self.sr_recalc_bars = 3

        # 儀表板
        self.dashboard = {
            "status":       "等待訊號",
            "trend_4h":     "─",
            "trend_1h":     "─",
            "trend_15m":    "─",
            "adx":          0.0,
            "atr":          0.0,
            "sr_zones":     [],
            "last_sweep":   None,
            "active_trade": None,
            "last_update":  None,
            "session_open": False,
            "event_paused": False,
            "next_event":   None,
        }

        logger.info(
            f"[SNR] 策略初始化 v2 | SR(15m:{sr_lookback_15m}/1h:{sr_lookback_1h}/4h:{sr_lookback_4h}) | "
            f"掃止損:ATR×{sweep_atr_mult} 量×{sweep_vol_mult} | RR≥{min_rr} | "
            f"ADX≥{adx_threshold} | 部分平倉:{int(partial_close_ratio*100)}% | Pin Bar比:{pin_bar_ratio}"
        )

    # ──────────────────────────────────────────
    # 主循環入口
    # ──────────────────────────────────────────

    async def on_bar(self, symbol: str, df: pd.DataFrame):
        if symbol != self.contract_id:
            return
        if self._is_processing or len(df) < self.atr_period + 10:
            return

        try:
            self._is_processing = True

            # 讀 bar_store 更長歷史
            try:
                bs = getattr(self.engine, 'bar_store', None)
                if bs is not None:
                    df_long = bs.load(self.contract_id, "15m", limit=500)
                    if not df_long.empty and len(df_long) > len(df):
                        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
                            df_long.index = df_long.index.tz_convert(None)
                        df = df_long
            except Exception as _e:
                logger.warning(f"[SNR] bar_store 讀取失敗: {_e}")

            # 每日載入事件
            await self._load_events_if_needed()

            # 更新 SR 冷卻倒數
            self._tick_sr_cooldown()

            # 事件後等待 bar 計數
            if self._event_bars_after > 0:
                self._event_bars_after -= 1
                if self._event_bars_after == 0:
                    self._event_paused = False
                    logger.info("[SNR] 事件後等待完成，恢復交易")
                    await self.notifier.send_message("✅ *事件後等待完成，策略恢復交易*")

            paused = getattr(self.engine, 'strategy_paused', False)

            # 有持倉：管理移動止盈 + 部分平倉
            if self._active_trade and self.get_pos_size() != 0:
                await self._manage_trade(df)
                self._update_dashboard(df)
                return

            # 持倉消失：清除追蹤
            if self._active_trade and self.get_pos_size() == 0:
                logger.info("[SNR] 持倉已平，清除追蹤")
                self._active_trade = None

            if paused or self._event_paused:
                status = "已暫停（事件）" if self._event_paused else "已暫停"
                self.dashboard["status"] = status
                self._update_dashboard(df)
                return

            # 環境偵測（無論時段都持續計算 ATR / SR / 趨勢）
            await self._update_environment(df)

            # 時段過濾 + 事件過濾：只阻止進場，不影響環境偵測
            session_ok = self._is_session_open()
            event_block = self._check_event_pause()

            if not session_ok:
                self.dashboard["status"] = "休市時段"
            elif event_block:
                pass  # _check_event_pause 已設定狀態
            elif not paused:
                # 尋找進場
                current_bar_idx = len(df) - 1
                if current_bar_idx != self._last_signal_bar:
                    await self._find_entry(df, current_bar_idx)

                # 5M 精確化
                if self._pending_5m_signal and self.use_5m_confirm:
                    await self._check_5m_confirm()

            self._update_dashboard(df)

        except Exception as e:
            import traceback
            logger.error("[SNR] on_bar 異常: %s | %s", e, traceback.format_exc())
        finally:
            self._is_processing = False

    # ──────────────────────────────────────────
    # 環境偵測（休市也持續更新）
    # ──────────────────────────────────────────

    async def _update_environment(self, df: pd.DataFrame):
        """隨時計算 ATR / SR / 多時框趨勢，更新 dashboard，不受時段限制"""
        try:
            atr = self._calc_atr(df)
            self.dashboard["atr"] = round(atr, 2)

            # SR 計算（快取）
            bar_idx = len(df) - 1
            if bar_idx - self._sr_last_bar >= self.sr_recalc_bars or not self._sr_cache:
                sr_zones = self._calc_sr_zones(df)
                self._sr_cache = sr_zones
                self._sr_last_bar = bar_idx

            # 同步到 dashboard
            logger.info(f"[SNR] _update_environment SR 結果: {len(self._sr_cache)} 個區域 | df長度:{len(df)} | ATR:{atr:.2f}")
            sr_display = []
            for z in self._sr_cache[:6]:
                cooldown = self._sr_cooldown.get(z.mid, 0)
                sr_display.append({
                    "top": z.top, "bottom": z.bottom,
                    "strength": round(z.strength, 3),
                    "tf": z.timeframe,
                    "cooldown": cooldown,
                })
            self.dashboard["sr_zones"] = sr_display

            # 多時框趨勢
            trend_4h  = self._get_htf_trend("4h")
            trend_1h  = self._get_htf_trend("1h")
            trend_15m_bull = self._check_structure(df, "bullish")
            trend_15m_bear = self._check_structure(df, "bearish")

            self.dashboard["trend_4h"]  = {"bull": "🟢多", "bear": "🔴空", "neutral": "⚪中性"}[trend_4h]
            self.dashboard["trend_1h"]  = {"bull": "🟢多", "bear": "🔴空", "neutral": "⚪中性"}[trend_1h]
            self.dashboard["trend_15m"] = "🟢多" if trend_15m_bull else ("🔴空" if trend_15m_bear else "⚪中性")

            # ADX
            try:
                import talib, numpy as np
                adx_vals = talib.ADX(df['h'].values, df['l'].values, df['c'].values, timeperiod=self.adx_period)
                self.dashboard["adx"] = round(float(adx_vals[-1]), 1) if not np.isnan(adx_vals[-1]) else 0.0
            except Exception:
                pass

            self.dashboard["session_open"] = self._is_session_open()

        except Exception as e:
            pass  # 環境偵測失敗不影響系統

    # ──────────────────────────────────────────
    # 時段過濾
    # ──────────────────────────────────────────

    def _is_session_open(self) -> bool:
        now = datetime.now(timezone.utc)
        h = now.hour + now.minute / 60
        london_open = self.session_london[0] <= h < self.session_london[1]
        ny_open     = self.session_ny[0] <= h < self.session_ny[1]
        return london_open or ny_open

    # ──────────────────────────────────────────
    # 事件過濾
    # ──────────────────────────────────────────

    async def _load_events_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._events_loaded_date == today:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
                if resp.status_code == 200:
                    raw = resp.json()
                    self._events = []
                    for e in raw:
                        if e.get("impact") != "High":
                            continue
                        try:
                            ts = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                            self._events.append(EconomicEvent(
                                title=e.get("title", ""),
                                timestamp=ts,
                                impact=e.get("impact", "")
                            ))
                        except Exception:
                            continue
                    self._events_loaded_date = today
                    logger.info(f"[SNR] 今週高影響事件: {len(self._events)} 個")
        except Exception as e:
            logger.warning(f"[SNR] 事件載入失敗（不影響交易）: {e}")

    def _check_event_pause(self) -> bool:
        now = datetime.now(timezone.utc)
        for ev in self._events:
            diff_min = (ev.timestamp - now).total_seconds() / 60
            # 事件前 15 分鐘
            if 0 <= diff_min <= self.event_pause_before:
                if not self._event_paused:
                    self._event_paused = True
                    self._event_bars_after = self.event_resume_after_bars
                    logger.warning(f"[SNR] 事件暫停: {ev.title} 距今 {diff_min:.0f} 分鐘")
                    asyncio.create_task(self.notifier.send_message(
                        f"⏸ *事件暫停*\n`{ev.title}` 距今 {diff_min:.0f} 分鐘\n事件後等 {self.event_resume_after_bars} 根K棒恢復"
                    ))
                self.dashboard["event_paused"] = True
                self.dashboard["next_event"] = ev.title
                return True
        self.dashboard["event_paused"] = False
        return False

    # ──────────────────────────────────────────
    # 盤整過濾
    # ──────────────────────────────────────────

    def _is_trending(self, df: pd.DataFrame) -> bool:
        if len(df) < self.adx_period + self.range_lookback + 5:
            return False
        atr = self._calc_atr(df)
        if atr <= 0:
            return False

        # ADX > threshold
        try:
            adx_vals = talib.ADX(df['h'].values, df['l'].values, df['c'].values, timeperiod=self.adx_period)
            adx = float(adx_vals[-1])
            self.dashboard["adx"] = round(adx, 1)
        except Exception:
            adx = 0.0

        if adx < self.adx_threshold:
            return False

        # 近 range_lookback 根的高低點範圍 > ATR × range_atr_mult
        recent = df.tail(self.range_lookback)
        price_range = recent['h'].max() - recent['l'].min()
        return price_range > atr * self.range_atr_mult

    # ──────────────────────────────────────────
    # 多時框趨勢
    # ──────────────────────────────────────────

    def _get_htf_trend(self, timeframe: str) -> str:
        """
        回傳 'bull' | 'bear' | 'neutral'
        從 bar_store 讀對應時框的 df
        """
        try:
            bs = getattr(self.engine, 'bar_store', None)
            if bs is None:
                return "neutral"
            limit = {"1h": 100, "4h": 60}.get(timeframe, 100)
            df_htf = bs.load(self.contract_id, timeframe, limit=limit)
            if df_htf.empty or len(df_htf) < 10:
                return "neutral"
            if hasattr(df_htf.index, 'tz') and df_htf.index.tz is not None:
                df_htf.index = df_htf.index.tz_convert(None)

            bull = self._check_structure(df_htf, "bullish")
            bear = self._check_structure(df_htf, "bearish")
            if bull and not bear:
                return "bull"
            elif bear and not bull:
                return "bear"
            return "neutral"
        except Exception:
            return "neutral"

    # ──────────────────────────────────────────
    # 型態確認
    # ──────────────────────────────────────────

    def _is_pin_bar(self, df: pd.DataFrame, idx: int, side: str) -> bool:
        row = df.iloc[idx]
        o, h, l, c = row['o'] if 'o' in df.columns else row['c'], row['h'], row['l'], row['c']
        body = abs(c - o) if 'o' in df.columns else 0.001
        if body == 0:
            body = (h - l) * 0.1

        if side == "bullish":
            lower_shadow = min(o, c) - l if 'o' in df.columns else c - l
            return lower_shadow >= body * self.pin_bar_ratio
        else:
            upper_shadow = h - max(o, c) if 'o' in df.columns else h - c
            return upper_shadow >= body * self.pin_bar_ratio

    def _is_engulfing(self, df: pd.DataFrame, idx: int, side: str) -> bool:
        if idx < 1:
            return False
        prev = df.iloc[idx - 1]
        curr = df.iloc[idx]
        if 'o' not in df.columns:
            return False
        if side == "bullish":
            # 當根陽線吞掉前根陰線
            return (curr['c'] > curr['o'] and
                    prev['c'] < prev['o'] and
                    curr['c'] > prev['o'] and
                    curr['o'] < prev['c'])
        else:
            # 當根陰線吞掉前根陽線
            return (curr['c'] < curr['o'] and
                    prev['c'] > prev['o'] and
                    curr['c'] < prev['o'] and
                    curr['o'] > prev['c'])

    def _check_candle_pattern(self, df: pd.DataFrame, idx: int, side: str) -> bool:
        """Pin Bar 或 吞噬，其中一個滿足即可。pin_bar_ratio=0 時跳過型態確認"""
        if self.pin_bar_ratio <= 0:
            return True   # 型態確認已停用
        pin = self._is_pin_bar(df, idx, side)
        eng = self._is_engulfing(df, idx, side) or (
            idx + 1 < len(df) and self._is_engulfing(df, idx + 1, side)
        )
        return pin or eng

    # ──────────────────────────────────────────
    # SR 冷卻
    # ──────────────────────────────────────────

    def _tick_sr_cooldown(self):
        keys = list(self._sr_cooldown.keys())
        for k in keys:
            self._sr_cooldown[k] -= 1
            if self._sr_cooldown[k] <= 0:
                del self._sr_cooldown[k]

    def _is_sr_on_cooldown(self, zone: SRZone) -> bool:
        return zone.mid in self._sr_cooldown

    def _set_sr_cooldown(self, zone: SRZone):
        self._sr_cooldown[zone.mid] = self.sr_cooldown_bars

    # ──────────────────────────────────────────
    # 5M 精確化
    # ──────────────────────────────────────────

    async def _check_5m_confirm(self):
        if not self._pending_5m_signal:
            return
        sig = self._pending_5m_signal
        # 超時（超過 3 根 15M K 棒）
        if sig.get("expires_bar", 0) < self._last_signal_bar:
            logger.info("[SNR] 5M 確認超時，放棄訊號")
            self._pending_5m_signal = None
            return
        try:
            bs = getattr(self.engine, 'bar_store', None)
            if bs is None:
                return
            df_5m = bs.load(self.contract_id, "5m", limit=30)
            if df_5m.empty:
                return
            if hasattr(df_5m.index, 'tz') and df_5m.index.tz is not None:
                df_5m.index = df_5m.index.tz_convert(None)

            side = sig["side"]
            # 5M 出現同向結構確認
            confirmed = self._check_structure(df_5m, "bullish" if side == "long" else "bearish")
            if confirmed:
                logger.info(f"[SNR] 5M 確認 {side} 訊號，進場")
                await self._place_order(
                    side=OrderSide.BUY if side == "long" else OrderSide.SELL,
                    sl_ticks=sig["sl_ticks"],
                    tp_ticks=sig["tp_ticks"],
                    sl_price=sig["sl_price"],
                    tp_price=sig["tp_price"],
                    atr=sig["atr"],
                    near_zone=None
                )
                self._pending_5m_signal = None
        except Exception as e:
            logger.warning(f"[SNR] 5M 確認失敗: {e}")

    # ──────────────────────────────────────────
    # 進場邏輯
    # ──────────────────────────────────────────

    async def _find_entry(self, df: pd.DataFrame, bar_idx: int):
        atr = self._calc_atr(df)
        if atr <= 0:
            return

        current_price = df['c'].iloc[-1]

        # 盤整過濾
        if not self._is_trending(df):
            self.dashboard["status"] = "盤整中，等待趨勢"
            return

        # SR 計算（快取）
        if bar_idx - self._sr_last_bar >= self.sr_recalc_bars or not self._sr_cache:
            sr_zones = self._calc_sr_zones(df)
            self._sr_cache = sr_zones
            self._sr_last_bar = bar_idx
        else:
            sr_zones = self._sr_cache

        if not sr_zones:
            return

        near_zone = self._nearest_zone(current_price, sr_zones)
        if not near_zone:
            return

        # SR 冷卻過濾
        if self._is_sr_on_cooldown(near_zone):
            self.dashboard["status"] = f"SR 冷卻中（{self._sr_cooldown.get(near_zone.mid, 0)}根）"
            return

        # 多時框趨勢（直接讀 _update_environment 已算好的結果）
        trend_4h  = self._get_htf_trend("4h")
        trend_1h  = self._get_htf_trend("1h")
        trend_15m_bull = self._check_structure(df, "bullish")
        trend_15m_bear = self._check_structure(df, "bearish")

        # 流動性掠奪偵測
        sweep = self._detect_sweep(df, bar_idx, atr)
        if not sweep:
            self.dashboard["status"] = "等待訊號"
            return

        # 型態確認
        pattern_ok = self._check_candle_pattern(df, sweep.recover_bar_idx, sweep.side)
        if not pattern_ok:
            logger.info(f"[SNR] 掃止損確認，但無 Pin Bar/吞噬型態，跳過")
            return

        sweep.is_pin_bar   = self._is_pin_bar(df, sweep.recover_bar_idx, sweep.side)
        sweep.is_engulfing = self._is_engulfing(df, sweep.recover_bar_idx, sweep.side)

        # 做多條件：4H 不空頭 + 1H 多頭或中性 + 15M 多頭 + 假跌破
        if sweep.side == "bullish":
            if trend_4h == "bear":
                logger.info("[SNR] 做多訊號但 4H 空頭，跳過")
                return
            if trend_1h == "bear":
                logger.info("[SNR] 做多訊號但 1H 空頭，跳過")
                return
            if not trend_15m_bull:
                logger.info("[SNR] 做多訊號但 15M 無多頭結構，跳過")
                return
            await self._evaluate_long(df, sweep, sr_zones, current_price, atr, bar_idx, near_zone)

        # 做空條件：4H 不多頭 + 1H 空頭或中性 + 15M 空頭 + 假突破
        elif sweep.side == "bearish":
            if trend_4h == "bull":
                logger.info("[SNR] 做空訊號但 4H 多頭，跳過")
                return
            if trend_1h == "bull":
                logger.info("[SNR] 做空訊號但 1H 多頭，跳過")
                return
            if not trend_15m_bear:
                logger.info("[SNR] 做空訊號但 15M 無空頭結構，跳過")
                return
            await self._evaluate_short(df, sweep, sr_zones, current_price, atr, bar_idx, near_zone)

    # ──────────────────────────────────────────
    # 進場評估
    # ──────────────────────────────────────────

    async def _evaluate_long(self, df, sweep, sr_zones, current_price, atr, bar_idx, near_zone):
        sl_price    = sweep.shadow_tip - (self.sl_buffer_ticks * self.tick_size)
        sl_distance = current_price - sl_price
        if sl_distance <= 0:
            return

        tp_zone = self._next_zone_above(current_price, sr_zones)
        if not tp_zone:
            return
        tp_price    = tp_zone.bottom
        tp_distance = tp_price - current_price
        rr          = tp_distance / sl_distance

        if rr < self.min_rr:
            logger.info(f"[SNR] 做多 RR={rr:.2f} 不足 {self.min_rr}，跳過")
            return

        account = DataHub.get_account()
        if not account:
            return
        max_risk = account.balance * self.max_risk_pct
        risk_per_contract = sl_distance * 2
        if risk_per_contract > max_risk:
            logger.info(f"[SNR] 做多風險 ${risk_per_contract:.0f} 超限 ${max_risk:.0f}，跳過")
            return

        sl_ticks = int(round(sl_distance / self.tick_size)) + self.sl_buffer_ticks
        tp_ticks = int(round(tp_distance / self.tick_size))

        logger.info(
            f"[SNR] 🟢 做多訊號 | 價:{current_price:.2f} SL:{sl_price:.2f} TP:{tp_price:.2f} "
            f"RR:{rr:.2f} | {'Pin' if sweep.is_pin_bar else ''} {'Eng' if sweep.is_engulfing else ''}"
        )

        if self.use_5m_confirm:
            self._pending_5m_signal = {
                "side": "long", "sl_ticks": sl_ticks, "tp_ticks": tp_ticks,
                "sl_price": sl_price, "tp_price": tp_price, "atr": atr,
                "near_zone": near_zone, "expires_bar": bar_idx + 3
            }
            logger.info("[SNR] 等待 5M 確認...")
        else:
            await self._place_order(OrderSide.BUY, sl_ticks, tp_ticks, sl_price, tp_price, atr, near_zone)

        self._last_signal_bar = bar_idx

    async def _evaluate_short(self, df, sweep, sr_zones, current_price, atr, bar_idx, near_zone):
        sl_price    = sweep.shadow_tip + (self.sl_buffer_ticks * self.tick_size)
        sl_distance = sl_price - current_price
        if sl_distance <= 0:
            return

        tp_zone = self._next_zone_below(current_price, sr_zones)
        if not tp_zone:
            return
        tp_price    = tp_zone.top
        tp_distance = current_price - tp_price
        rr          = tp_distance / sl_distance

        if rr < self.min_rr:
            logger.info(f"[SNR] 做空 RR={rr:.2f} 不足 {self.min_rr}，跳過")
            return

        account = DataHub.get_account()
        if not account:
            return
        max_risk = account.balance * self.max_risk_pct
        risk_per_contract = sl_distance * 2
        if risk_per_contract > max_risk:
            logger.info(f"[SNR] 做空風險 ${risk_per_contract:.0f} 超限 ${max_risk:.0f}，跳過")
            return

        sl_ticks = int(round(sl_distance / self.tick_size)) + self.sl_buffer_ticks
        tp_ticks = int(round(tp_distance / self.tick_size))

        logger.info(
            f"[SNR] 🔴 做空訊號 | 價:{current_price:.2f} SL:{sl_price:.2f} TP:{tp_price:.2f} "
            f"RR:{rr:.2f} | {'Pin' if sweep.is_pin_bar else ''} {'Eng' if sweep.is_engulfing else ''}"
        )

        if self.use_5m_confirm:
            self._pending_5m_signal = {
                "side": "short", "sl_ticks": sl_ticks, "tp_ticks": tp_ticks,
                "sl_price": sl_price, "tp_price": tp_price, "atr": atr,
                "near_zone": near_zone, "expires_bar": bar_idx + 3
            }
            logger.info("[SNR] 等待 5M 確認...")
        else:
            await self._place_order(OrderSide.SELL, sl_ticks, tp_ticks, sl_price, tp_price, atr, near_zone)

        self._last_signal_bar = bar_idx

    # ──────────────────────────────────────────
    # 持倉管理（部分止盈 + 移動止盈）
    # ──────────────────────────────────────────

    async def _manage_trade(self, df: pd.DataFrame):
        trade = self._active_trade
        current_price  = df['c'].iloc[-1]
        sl_distance    = abs(trade.entry_price - trade.sl_price)
        trigger_dist   = sl_distance * self.trailing_rr_trigger

        if trade.side == "long":
            profit = current_price - trade.entry_price

            # 部分止盈（先平 50%）
            if not trade.partial_closed and profit >= sl_distance * 1.0:
                partial_size = max(1, int(self.size * self.partial_close_ratio))
                logger.info(f"[SNR] 💰 部分止盈 {partial_size}口 | 浮盈:{profit:.2f} = 1R")
                await self.ts.close_position(self.account_id, self.contract_id)
                trade.partial_closed = True
                await self.notifier.send_message(
                    f"💰 *部分止盈*\n方向：多 🟢\n平倉：{partial_size}口\n浮盈：+{profit:.2f} = 1R"
                )

            # 移動止盈
            if profit >= trigger_dist and not trade.trailing_active:
                trade.trailing_active = True
                logger.info(f"[SNR] 🔔 移動止盈啟動 | {profit:.2f} ≥ {trigger_dist:.2f}")

            if trade.trailing_active:
                new_tp = self._find_trailing_support(df, "long")
                if new_tp and new_tp > trade.trailing_tp:
                    logger.info(f"[SNR] 📈 移動止盈上移 {trade.trailing_tp:.2f} → {new_tp:.2f}")
                    trade.trailing_tp = new_tp
                if current_price <= trade.trailing_tp:
                    logger.info(f"[SNR] 🏳️ 移動止盈觸發 | 價:{current_price:.2f}")
                    await self.ts.close_position(self.account_id, self.contract_id)
                    self._active_trade = None

        else:  # short
            profit = trade.entry_price - current_price

            if not trade.partial_closed and profit >= sl_distance * 1.0:
                partial_size = max(1, int(self.size * self.partial_close_ratio))
                logger.info(f"[SNR] 💰 部分止盈 {partial_size}口 | 浮盈:{profit:.2f} = 1R")
                await self.ts.close_position(self.account_id, self.contract_id)
                trade.partial_closed = True
                await self.notifier.send_message(
                    f"💰 *部分止盈*\n方向：空 🔴\n平倉：{partial_size}口\n浮盈：+{profit:.2f} = 1R"
                )

            if profit >= trigger_dist and not trade.trailing_active:
                trade.trailing_active = True
                logger.info(f"[SNR] 🔔 移動止盈啟動 | {profit:.2f} ≥ {trigger_dist:.2f}")

            if trade.trailing_active:
                new_tp = self._find_trailing_support(df, "short")
                if new_tp and new_tp < trade.trailing_tp:
                    logger.info(f"[SNR] 📉 移動止盈下移 {trade.trailing_tp:.2f} → {new_tp:.2f}")
                    trade.trailing_tp = new_tp
                if current_price >= trade.trailing_tp:
                    logger.info(f"[SNR] 🏳️ 移動止盈觸發 | 價:{current_price:.2f}")
                    await self.ts.close_position(self.account_id, self.contract_id)
                    self._active_trade = None

    def _find_trailing_support(self, df: pd.DataFrame, side: str) -> Optional[float]:
        window = df.tail(self.sweep_lookback)
        if side == "long":
            lows = window['l'].values
            for i in range(len(lows) - 2, 0, -1):
                if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                    return float(lows[i])
        else:
            highs = window['h'].values
            for i in range(len(highs) - 2, 0, -1):
                if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                    return float(highs[i])
        return None

    # ──────────────────────────────────────────
    # 下單
    # ──────────────────────────────────────────

    async def _place_order(self, side: OrderSide, sl_ticks: int, tp_ticks: int,
                           sl_price: float, tp_price: float, atr: float, near_zone):
        order_id = await self.ts.place_order(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=OrderType.MARKET,
            side=int(side),
            size=self.size,
            sl_ticks=sl_ticks,
            tp_ticks=tp_ticks,
        )
        if not order_id:
            logger.error("[SNR] 下單失敗")
            return

        trade_side = "long" if side == OrderSide.BUY else "short"
        entry_price = DataHub.get_bars(self.contract_id)['c'].iloc[-1] if not DataHub.get_bars(self.contract_id).empty else 0.0

        self._active_trade = ActiveTrade(
            side=trade_side,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            trailing_tp=tp_price,
            sl_ticks=sl_ticks,
            risk_amount=abs(entry_price - sl_price) * 2,
            order_id=order_id,
            sr_zone_mid=near_zone.mid if near_zone else 0.0,
        )

        # 設定 SR 冷卻（止損後才生效，這裡先記錄區域）
        self.dashboard["active_trade"] = self._active_trade

        side_str = "多 🟢" if trade_side == "long" else "空 🔴"
        await self.notifier.send_message(
            f"📋 *SNR 進場通知*\n"
            f"方向：{side_str}\n"
            f"進場：`{entry_price:.2f}`\n"
            f"止損：`{sl_price:.2f}` ({sl_ticks} ticks)\n"
            f"止盈：`{tp_price:.2f}` ({tp_ticks} ticks)\n"
            f"訂單：`#{order_id}`"
        )

    # ──────────────────────────────────────────
    # 流動性掠奪偵測
    # ──────────────────────────────────────────

    def _detect_sweep(self, df: pd.DataFrame, bar_idx: int, atr: float) -> Optional[LiquiditySweep]:
        window    = df.tail(self.sweep_lookback + 5)
        highs     = window['h'].values
        lows      = window['l'].values
        closes    = window['c'].values
        vols      = window['v'].values
        N         = len(window)
        avg_vol   = np.mean(vols[-self.sweep_lookback:]) if len(vols) >= self.sweep_lookback else np.mean(vols)
        min_move  = atr * self.sweep_atr_mult

        for i in range(N - 3, N - 1):
            # 假跌破做多（bullish sweep）
            recent_low = np.min(lows[max(0, i - self.sweep_lookback):i])
            if (lows[i] < recent_low and
                    recent_low - lows[i] >= min_move and
                    closes[i] > recent_low and
                    vols[i] >= avg_vol * self.sweep_vol_mult):
                return LiquiditySweep(
                    side="bullish",
                    sweep_price=recent_low,
                    sweep_bar_idx=bar_idx - (N - 1 - i),
                    recover_bar_idx=bar_idx - (N - 1 - i),
                    shadow_tip=lows[i],
                    atr=atr
                )

            # 假突破做空（bearish sweep）
            recent_high = np.max(highs[max(0, i - self.sweep_lookback):i])
            if (highs[i] > recent_high and
                    highs[i] - recent_high >= min_move and
                    closes[i] < recent_high and
                    vols[i] >= avg_vol * self.sweep_vol_mult):
                return LiquiditySweep(
                    side="bearish",
                    sweep_price=recent_high,
                    sweep_bar_idx=bar_idx - (N - 1 - i),
                    recover_bar_idx=bar_idx - (N - 1 - i),
                    shadow_tip=highs[i],
                    atr=atr
                )
        return None

    # ──────────────────────────────────────────
    # 結構判斷
    # ──────────────────────────────────────────

    def _check_structure(self, df: pd.DataFrame, side: str) -> bool:
        window = df.tail(self.sweep_lookback * 2)
        highs  = window['h'].values
        lows   = window['l'].values
        sl     = self.sr_side_len
        local_highs, local_lows = [], []

        for i in range(sl, len(window) - sl):
            if all(highs[i] >= highs[i - j] for j in range(1, sl + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, sl + 1)):
                local_highs.append(highs[i])
            if all(lows[i] <= lows[i - j] for j in range(1, sl + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, sl + 1)):
                local_lows.append(lows[i])

        if side == "bullish" and len(local_lows) >= 2:
            return local_lows[-1] > local_lows[-2]
        elif side == "bearish" and len(local_highs) >= 2:
            return local_highs[-1] < local_highs[-2]
        return False

    # ──────────────────────────────────────────
    # SR 計算（v2）
    # ──────────────────────────────────────────

    def _calc_sr_zones(self, df: pd.DataFrame, timeframe: str = "15m") -> List[SRZone]:
        lookback = self.sr_lookback.get(timeframe, 150)
        if len(df) < lookback:
            return []

        window = df.tail(lookback).copy()
        highs  = window['h'].values
        lows   = window['l'].values
        vols   = window['v'].values
        N      = len(window)
        atr    = self._calc_atr(df)
        zone_thickness = max(atr * self.sr_atr_mult, 0.5)
        avg_vol = np.mean(vols) if np.mean(vols) > 0 else 1.0

        fractal_prices, fractal_energies = [], []
        sl = self.sr_side_len

        for i in range(sl, N - sl):
            pos_ratio   = i / N
            time_factor = pos_ratio ** self.sr_time_decay
            vol_factor  = 1.0 + (vols[i] / avg_vol - 1) * self.sr_vol_weight
            energy      = vol_factor * time_factor

            if all(highs[i] >= highs[i - j] for j in range(1, sl + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, sl + 1)):
                fractal_prices.append(highs[i])
                fractal_energies.append(energy)

            if all(lows[i] <= lows[i - j] for j in range(1, sl + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, sl + 1)):
                fractal_prices.append(lows[i])
                fractal_energies.append(energy)

        if not fractal_prices:
            return []

        price_min = min(lows)
        price_max = max(highs)
        n_bins    = max(int((price_max - price_min) / zone_thickness), 1)
        bin_weights = np.zeros(n_bins)

        for price, energy in zip(fractal_prices, fractal_energies):
            bin_idx = min(int((price - price_min) / zone_thickness), n_bins - 1)
            bin_weights[bin_idx] += energy

        max_energy = bin_weights.max()
        nonzero    = bin_weights[bin_weights > 0]
        avg_energy = nonzero.mean() if len(nonzero) > 0 else 0
        closes     = window['c'].values
        current_close = closes[-1]

        # 門檻：超過平均能量 × sensitivity 倍的區域都納入
        # （移除 max*0.9 的過嚴條件，改以相對強度排名取前 N）
        zones = []
        threshold = avg_energy * self.sr_sensitivity if avg_energy > 0 else 0
        for i, w in enumerate(bin_weights):
            if w > threshold:
                bottom = price_min + i * zone_thickness
                top    = bottom + zone_thickness
                zones.append(SRZone(
                    top=round(top, 2),
                    bottom=round(bottom, 2),
                    strength=float(w),
                    timeframe=timeframe,
                ))

        zones.sort(key=lambda z: z.strength, reverse=True)
        return zones[:10]

    # ──────────────────────────────────────────
    # 輔助
    # ──────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame) -> float:
        if len(df) < self.atr_period + 1:
            return 0.0
        try:
            atr_vals = talib.ATR(df['h'].values, df['l'].values, df['c'].values, timeperiod=self.atr_period)
            return float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
        except Exception:
            w = df.tail(self.atr_period + 1).copy()
            w['prev_c'] = w['c'].shift(1)
            w['tr'] = w.apply(lambda r: max(r['h'] - r['l'],
                                             abs(r['h'] - r['prev_c']) if pd.notna(r['prev_c']) else 0,
                                             abs(r['l'] - r['prev_c']) if pd.notna(r['prev_c']) else 0), axis=1)
            return float(w['tr'].iloc[1:].mean())

    def _nearest_zone(self, price: float, zones: List[SRZone]) -> Optional[SRZone]:
        touching = [z for z in zones if z.bottom <= price <= z.top]
        if touching:
            return max(touching, key=lambda z: z.strength)
        nearby = [z for z in zones if abs(z.mid - price) <= self._calc_atr_simple(price) * 0.5]
        if nearby:
            return min(nearby, key=lambda z: abs(z.mid - price))
        return None

    def _calc_atr_simple(self, price: float) -> float:
        return self.dashboard.get("atr", price * 0.005)

    def _next_zone_above(self, price: float, zones: List[SRZone]) -> Optional[SRZone]:
        above = [z for z in zones if z.bottom > price]
        return min(above, key=lambda z: z.bottom) if above else None

    def _next_zone_below(self, price: float, zones: List[SRZone]) -> Optional[SRZone]:
        below = [z for z in zones if z.top < price]
        return max(below, key=lambda z: z.top) if below else None

    def get_pos_size(self) -> int:
        pos = DataHub.get_position(self.contract_id)
        return pos.size if pos else 0

    # ──────────────────────────────────────────
    # 儀表板更新
    # ──────────────────────────────────────────

    def _update_dashboard(self, df: pd.DataFrame):
        try:
            paused = getattr(self.engine, 'strategy_paused', False)
            atr    = self._calc_atr(df)
            sr_display = []
            for z in self._sr_cache[:6]:
                cooldown = self._sr_cooldown.get(z.mid, 0)
                sr_display.append({
                    "top": z.top, "bottom": z.bottom,
                    "strength": round(z.strength, 3),
                    "tf": z.timeframe,
                    "cooldown": cooldown,
                })

            if self._active_trade:
                t = self._active_trade
                status = f"持倉 {'多🟢' if t.side=='long' else '空🔴'}"
                if t.partial_closed:
                    status += " 已部分平倉"
                if t.trailing_active:
                    status += " 🔔移動止盈"
            elif paused:
                status = "已暫停"
            elif self._event_paused:
                status = "事件暫停中"
            elif not self._is_session_open():
                status = "休市時段"
            else:
                status = self.dashboard.get("status", "等待訊號")

            self.dashboard.update({
                "status":       status,
                "atr":          round(atr, 2),
                "sr_zones":     sr_display,
                "active_trade": self._active_trade,
                "last_update":  datetime.utcnow(),
                "session_open": self._is_session_open(),
            })
        except Exception:
            pass
