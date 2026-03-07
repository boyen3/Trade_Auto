# services/daily_report.py
from datetime import datetime
from utils.logger import logger


class DailyReportService:
    """
    每日績效報表服務。
    在 OrderMonitor 或 main.py 的主循環裡每秒呼叫 check_and_send()，
    到達 UTC 22:00 時自動產生並發送當日報表。
    """
    def __init__(self, trading_service, notifier):
        self.ts = trading_service
        self.notifier = notifier
        self._last_report_date = None

    async def check_and_send(self, account_id: int):
        now = datetime.utcnow()
        if now.hour == 22 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")
            if self._last_report_date == today:
                return
            self._last_report_date = today
            await self._send_report(account_id, today)

    async def _send_report(self, account_id: int, date: str):
        try:
            trades = await self.ts.get_today_trades(account_id)
            completed = [t for t in trades if t.profitAndLoss is not None]
            wins      = [t for t in completed if t.profitAndLoss > 0]
            losses    = [t for t in completed if t.profitAndLoss <= 0]
            total_pnl  = sum(t.profitAndLoss for t in completed)
            total_fees = sum(t.fees or 0.0 for t in trades)
            net_pnl    = total_pnl - total_fees
            win_rate   = len(wins) / len(completed) * 100 if completed else 0.0

            best  = max((t.profitAndLoss for t in completed), default=0.0)
            worst = min((t.profitAndLoss for t in completed), default=0.0)

            result_emoji = "🟢" if net_pnl >= 0 else "🔴"
            logger.info(
                f"[DailyReport] {date} | 損益:${total_pnl:+.2f} | "
                f"{len(completed)}筆 | 勝率:{win_rate:.1f}%"
            )

            await self.notifier.send_message(
                f"{result_emoji} *每日績效報表*\n"
                f"`{date}`\n"
                f"━━━━━━━━━━━━━━━\n"
                f"毛損益：`${total_pnl:+.2f}`\n"
                f"手續費：`-${total_fees:.2f}`\n"
                f"淨損益：`${net_pnl:+.2f}`\n"
                f"━━━━━━━━━━━━━━━\n"
                f"交易次數：`{len(completed)}` 筆\n"
                f"勝率：`{win_rate:.1f}%` ({len(wins)}勝 {len(losses)}敗)\n"
                f"最佳單筆：`${best:+.2f}`\n"
                f"最差單筆：`${worst:+.2f}`"
            )
        except Exception as e:
            logger.error(f"[DailyReport] 發送失敗: {e}")
