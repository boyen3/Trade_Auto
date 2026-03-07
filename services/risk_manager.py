# services/risk_manager.py
import os
from datetime import datetime
from core.data_hub import DataHub
from core.state_store import StateStore
from utils.logger import logger, log_risk_triggered


class RiskManager:
    def __init__(self, notifier=None):
        self.notifier = notifier

        # 從 .env 讀取所有風控參數
        self.max_daily_loss    = float(os.getenv("MAX_DAILY_LOSS", "1000.0"))
        self.max_daily_trades  = int(os.getenv("MAX_DAILY_TRADES", "20"))
        self.max_position_size = int(os.getenv("MAX_POSITION_SIZE", "3"))
        self.max_consec_losses = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
        self.min_balance       = float(os.getenv("MIN_ACCOUNT_BALANCE", "48000.0"))
        self.end_hour          = int(os.getenv("TRADING_END_HOUR_UTC", "21"))
        self.end_minute        = int(os.getenv("TRADING_END_MINUTE_UTC", "45"))
        self.close_hour        = int(os.getenv("FORCE_CLOSE_HOUR_UTC", "21"))
        self.close_minute      = int(os.getenv("FORCE_CLOSE_MINUTE_UTC", "55"))

        # 從 StateStore 恢復狀態（重啟後不歸零）
        self._store = StateStore()
        state = self._store.get_strategy_state("risk")
        self.daily_trades       = state.get("daily_trades", 0)
        self.consecutive_losses = state.get("consecutive_losses", 0)
        self._today             = state.get("date", "")
        self._risk_notified     = False  # 避免重複發 Telegram

        self._reset_if_new_day()
        logger.info(
            f"[RiskManager] 啟動 | 每日最大虧損: ${self.max_daily_loss} | "
            f"每日交易上限: {self.max_daily_trades} | "
            f"最大持倉: {self.max_position_size}口 | "
            f"連虧上限: {self.max_consec_losses} | "
            f"收盤時間: {self.end_hour:02d}:{self.end_minute:02d} UTC"
        )

    def _reset_if_new_day(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self._today != today:
            self.daily_trades = 0
            self.consecutive_losses = 0
            self._today = today
            self._risk_notified = False
            self._save()

    def _save(self):
        self._store.update_strategy_state("risk", {
            "daily_trades": self.daily_trades,
            "consecutive_losses": self.consecutive_losses,
            "date": self._today
        })

    def record_trade(self, pnl: float):
        """每筆交易結束後呼叫，更新計數器並持久化"""
        self._reset_if_new_day()
        self.daily_trades += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self._save()
        logger.info(
            f"[RiskManager] 交易記錄 | 今日第 {self.daily_trades} 筆 | "
            f"損益: ${pnl:+.2f} | 連續虧損: {self.consecutive_losses}"
        )

    def check_safety(self) -> tuple:
        """
        下單前呼叫。
        回傳 (True, "OK") 代表可以下單，
        回傳 (False, "原因") 代表禁止下單。
        """
        self._reset_if_new_day()
        acc = DataHub.account_info

        if not acc:
            return False, "帳戶數據未就緒"

        # 1. 每日最大虧損
        total_pnl = acc.realized_pnl + sum(
            p.unrealized_pnl_usd for p in DataHub.positions.values()
        )
        if total_pnl <= -self.max_daily_loss:
            reason = f"每日虧損已達上限 ${self.max_daily_loss}"
            self._trigger(reason, total_pnl, self.max_daily_loss)
            return False, reason

        # 2. 帳戶淨值保護線
        if acc.balance < self.min_balance:
            reason = f"帳戶餘額低於保護線 ${self.min_balance:,.0f}"
            self._trigger(reason, total_pnl, self.min_balance)
            return False, reason

        # 3. 每日交易次數
        if self.daily_trades >= self.max_daily_trades:
            reason = f"今日交易次數已達上限 {self.max_daily_trades}"
            return False, reason

        # 4. 連續虧損保護
        if self.consecutive_losses >= self.max_consec_losses:
            reason = f"連續虧損 {self.consecutive_losses} 次，暫停交易"
            self._trigger(reason, total_pnl, 0)
            return False, reason

        # 5. 最大持倉口數
        total_size = sum(p.size for p in DataHub.positions.values())
        if total_size >= self.max_position_size:
            reason = f"持倉口數已達上限 {self.max_position_size}"
            return False, reason

        # 6. 週末不交易
        now = datetime.utcnow()
        if now.weekday() >= 5:
            return False, "週末休市"

        # 7. 收盤時間
        if (now.hour, now.minute) >= (self.end_hour, self.end_minute):
            return False, f"已過收盤時間 {self.end_hour:02d}:{self.end_minute:02d} UTC"

        return True, "OK"

    def is_force_close_time(self) -> bool:
        """判斷是否到了強制平倉時間"""
        now = datetime.utcnow()
        if now.weekday() >= 5:
            return False
        return (now.hour, now.minute) >= (self.close_hour, self.close_minute)

    def _trigger(self, reason: str, total_pnl: float, limit: float):
        """風控觸發，log + 發 Telegram（只發一次，避免洗版）"""
        log_risk_triggered(reason, total_pnl, limit)
        if not self._risk_notified and self.notifier:
            self._risk_notified = True
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(
                        self.notifier.notify_risk_triggered(reason, total_pnl, limit)
                    )
            except Exception:
                pass
