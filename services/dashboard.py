# services/dashboard.py
"""
Console 即時儀表板 v2
=====================
支援 SNR v2 策略的所有新欄位：
  - 4H / 1H / 15M 多時框趨勢
  - ADX 盤整指數
  - 交易時段狀態
  - 事件暫停狀態
  - SR 冷卻期顯示
  - 部分止盈狀態
"""

import asyncio
import os
from datetime import datetime
from core.data_hub import DataHub
from utils.logger import logger


def _clear():
    os.system('cls' if os.name == 'nt' else 'clear')


def _bar(value: float, max_val: float, width: int = 10, char: str = "█") -> str:
    if max_val <= 0:
        return "─" * width
    filled = int(min(value / max_val, 1.0) * width)
    return char * filled + "░" * (width - filled)


def _adx_bar(adx: float, width: int = 10) -> str:
    filled = int(min(adx / 50.0, 1.0) * width)
    if adx < 20:
        char = "░"
    elif adx < 40:
        char = "▒"
    else:
        char = "█"
    return char * filled + "·" * (width - filled)


class Dashboard:

    def __init__(self, engine, strategy, refresh_secs: float = 5.0):
        self.engine       = engine
        self.strategy     = strategy
        self.refresh_secs = refresh_secs
        self._running     = True

    def stop(self):
        self._running = False

    async def start(self):
        logger.info("[Dashboard] 儀表板啟動")
        while self._running:
            try:
                # 每次刷新時主動觸發環境偵測（不受 on_bar 頻率限制）
                await self._refresh_environment()
                self._render()
            except Exception:
                pass
            await asyncio.sleep(self.refresh_secs)

    async def _refresh_environment(self):
        """主動從 bar_store 讀取數據並更新策略環境，不依賴 on_bar 觸發"""
        try:
            strategy = self.strategy
            if strategy is None:
                return
            bs = getattr(self.engine, 'bar_store', None)
            if bs is None:
                return
            import pandas as pd
            df = bs.load(strategy.contract_id, "15m", limit=600)
            if df.empty or len(df) < 30:
                return
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_convert(None)
            await strategy._update_environment(df)
        except Exception:
            pass

    def _render(self):
        _clear()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        acc = DataHub.account_info
        positions = list(DataHub.positions.values())
        d   = getattr(self.strategy, 'dashboard', {})
        cfg = self.engine.config

        W = 62

        def divider(char="─"):
            return char * W

        def title(text):
            pad = (W - len(text) - 2) // 2
            return f"{'─' * pad} {text} {'─' * (W - pad - len(text) - 2)}"

        lines = []

        lines.append("╔" + "═" * W + "╗")
        lines.append(f"║{'ProjectX SNR v3 策略儀表板':^{W}}║")
        lines.append(f"║{now:^{W}}║")
        lines.append("╚" + "═" * W + "╝")

        session_open = d.get("session_open", False)
        event_paused = d.get("event_paused", False)
        next_event   = d.get("next_event", None)
        paused       = getattr(self.engine, 'strategy_paused', False)

        if event_paused:
            session_line = f"  ⏸  事件暫停中  {next_event or ''}"
        elif paused:
            session_line = "  ⏸  策略已手動暫停"
        elif session_open:
            session_line = "  🟢 交易時段開放中  (紐約盤 NY)"
        else:
            session_line = "  🔴 休市時段  (等待倫敦/紐約盤)"
        lines.append(divider())
        lines.append(session_line)

        lines.append(title("帳戶"))
        if acc:
            pos_size   = sum(p.size for p in positions)
            unrealized = sum(p.unrealized_pnl_usd for p in positions)
            lines.append(f"  帳戶   : {acc.name}")
            lines.append(f"  餘額   : ${acc.balance:>10,.2f}    已實現: ${acc.realized_pnl:>+8.2f}")
            lines.append(f"  浮盈   : ${unrealized:>+10.2f}   持倉口數: {pos_size}")
        else:
            lines.append("  帳戶數據尚未就緒...")

        lines.append(title("多時框趨勢"))
        trend_4h  = d.get("trend_4h",  "─")
        trend_1h  = d.get("trend_1h",  "─")
        trend_15m = d.get("trend_15m", "─")
        adx       = d.get("adx", 0.0)
        adx_bar_s = _adx_bar(adx)
        adx_label = "盤整" if adx < 20 else ("趨勢" if adx < 40 else "強趨勢")
        lines.append(f"  4H  : {trend_4h:<12}  1H  : {trend_1h:<12}  15M : {trend_15m}")
        lines.append(f"  ADX : {adx:>5.1f}  [{adx_bar_s}] {adx_label}")

        lines.append(title("策略狀態"))
        status   = d.get("status", "─")
        atr      = d.get("atr", 0.0)
        last_upd = d.get("last_update")
        upd_str  = last_upd.strftime("%H:%M:%S") if last_upd else "─"
        status_icon = "⏸" if (paused or event_paused) else ("📍" if "持倉" in status else "👁")
        lines.append(f"  {status_icon} 狀態  : {status}")
        lines.append(f"  合約   : {cfg.get('symbol', '─')}")
        lines.append(f"  ATR(14): {atr:.2f} 點   策略更新: {upd_str}")

        lines.append(title("支撐壓力區域"))
        sr_zones = d.get("sr_zones", [])

        current_price = 0.0
        if positions:
            current_price = getattr(positions[0], 'currentPrice', 0.0)
        if current_price == 0.0 and DataHub.bars:
            bars = list(DataHub.bars.values())
            if bars and not bars[0].empty:
                b = bars[0]
                # 相容 'c' 和 'close' 兩種欄位命名
                col = 'c' if 'c' in b.columns else ('close' if 'close' in b.columns else None)
                if col:
                    current_price = float(b[col].iloc[-1])
        # 若 DataHub 還沒數據，嘗試從 bar_store 讀
        if current_price == 0.0:
            try:
                bs = getattr(self.engine, 'bar_store', None)
                strategy = self.strategy
                if bs and strategy:
                    df_tmp = bs.load(strategy.contract_id, "15m", limit=1)
                    if not df_tmp.empty:
                        col = 'c' if 'c' in df_tmp.columns else 'close'
                        current_price = float(df_tmp[col].iloc[-1])
            except Exception:
                pass

        if sr_zones:
            max_s = max(z["strength"] for z in sr_zones) if sr_zones else 1
            lines.append(f"  {'中心價':>9}  {'區間':^20}  {'強度':^10}  {'狀態'}")
            lines.append(f"  {'·' * (W - 2)}")
            for z in sr_zones[:6]:
                mid      = (z["top"] + z["bottom"]) / 2
                bar      = _bar(z["strength"], max_s, 8)
                dist     = mid - current_price if current_price > 0 else 0
                if dist > 0:
                    label = f"壓 ↑+{dist:.0f}"
                elif dist < 0:
                    label = f"支 ↓-{abs(dist):.0f}"
                else:
                    label = "◀ 當前區間"
                touching = " ◀" if current_price > 0 and z["bottom"] <= current_price <= z["top"] else ""
                cooldown = z.get("cooldown", 0)
                cool_str = f" ❄{cooldown}根" if cooldown > 0 else ""
                lines.append(
                    f"  {mid:>9.2f}  [{z['bottom']:.2f} ~ {z['top']:.2f}]  [{bar}]  {label}{touching}{cool_str}"
                )
        else:
            lines.append("  SR 區域計算中...")

        if current_price > 0:
            lines.append(f"  {'─' * (W - 2)}")
            lines.append(f"  當前價格: {current_price:.2f}")

        trade = d.get("active_trade")
        if trade:
            lines.append(title("持倉明細"))
            side_str = "多 🟢" if trade.side == "long" else "空 🔴"
            pnl_pts  = (current_price - trade.entry_price) if trade.side == "long" \
                       else (trade.entry_price - current_price)
            pnl_usd  = pnl_pts * 2
            partial  = " ✅已部分平倉" if trade.partial_closed else ""
            lines.append(f"  方向   : {side_str}{partial}")
            lines.append(f"  進場價 : {trade.entry_price:.2f}   浮盈: ${pnl_usd:+.2f}")
            lines.append(f"  止損   : {trade.sl_price:.2f}")
            sl_dist = abs(trade.entry_price - trade.sl_price)
            trigger = (trade.entry_price + sl_dist * 1.5) if trade.side == "long" \
                      else (trade.entry_price - sl_dist * 1.5)
            if trade.trailing_active:
                trail_price = getattr(trade, 'trailing_stop', getattr(trade, 'trailing_tp', 0.0))
                lines.append(f"  移動SL : {trail_price:.2f}  🔔 啟動中")
            else:
                lines.append(f"  移動TP : 未啟動  (觸發價: {trigger:.2f})")
            lines.append(f"  原始TP : {trade.tp_price:.2f}")

        lines.append(title("風控"))
        rm = getattr(self.engine, 'risk_manager', None)
        if rm and acc:
            total_pnl   = acc.realized_pnl + sum(p.unrealized_pnl_usd for p in positions)
            safe, reason = rm.check_safety()
            safety_icon  = "✅" if safe else "🚨"
            lines.append(f"  {safety_icon} 今日損益: ${total_pnl:>+8.2f}  限制: -${rm.max_daily_loss:.0f}")
            lines.append(f"  今日交易: {rm.daily_trades}/{rm.max_daily_trades}  連虧: {rm.consecutive_losses}/{rm.max_consec_losses}")
            if not safe:
                lines.append(f"  ⚠️  風控觸發: {reason}")

        lines.append(divider())
        lines.append(f"  儀表板刷新: {self.refresh_secs:.0f}s  |  /help 查看指令")
        lines.append(divider())

        print("\n".join(lines))
