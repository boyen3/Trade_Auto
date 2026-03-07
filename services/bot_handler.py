# services/bot_handler.py
"""
Telegram Bot 指令處理器
========================
處理所有從 Telegram 收到的 / 指令。
透過注入 engine 取得系統狀態，不直接依賴全域變數。

支援指令：
  /help     顯示所有可用指令
  /status   帳戶餘額、持倉狀態、今日損益
  /risk     風控狀態（今日次數、連續虧損）
  /pause    暫停下新單（不平現有倉位）
  /resume   恢復下單
  /stop     停止策略主循環（不平倉）
  /close    立刻平倉所有持倉
"""
from datetime import datetime, timezone
from core.data_hub import DataHub
from utils.logger import logger


class BotCommandHandler:
    def __init__(self, engine):
        """
        :param engine: ProjectXEngine 實例，用來存取所有服務
        """
        self.engine = engine
        self.notifier = engine.notifier
        # 記錄啟動時間，用來過濾離線期間累積的舊指令
        self._startup_time = datetime.now(timezone.utc)

    async def handle(self, command: str, args: list, message_date: datetime = None):
        # 忽略系統啟動前發出的舊指令（Telegram 離線累積）
        if message_date and message_date < self._startup_time:
            logger.info(f"[Bot] 忽略舊指令（啟動前）: {command} @ {message_date.strftime('%H:%M:%S')}")
            return

        handlers = {
            "/help":   self._cmd_help,
            "/status": self._cmd_status,
            "/risk":   self._cmd_risk,
            "/pause":  self._cmd_pause,
            "/resume": self._cmd_resume,
            "/stop":   self._cmd_stop,
            "/close":  self._cmd_close,
            "/diag":   self._cmd_diag,
        }
        handler = handlers.get(command)
        if handler:
            await handler(args)
        else:
            await self.notifier.send_message(
                f"❓ 未知指令：`{command}`\n輸入 /help 查看所有指令"
            )

    # ──────────────────────────────────────────
    # 指令實作
    # ──────────────────────────────────────────

    async def _cmd_help(self, args):
        await self.notifier.send_message(
            "🤖 *ProjectX Bot 指令列表*\n"
            "━━━━━━━━━━━━━━━\n"
            "/status  — 帳戶與持倉狀態\n"
            "/risk    — 風控狀態\n"
            "/pause   — 暫停下新單\n"
            "/resume  — 恢復下單\n"
            "/close   — 立刻平倉所有持倉\n"
            "/stop    — 停止策略（不平倉）\n"
            "/diag    — 診斷數據狀態\n"
            "━━━━━━━━━━━━━━━"
        )

    async def _cmd_status(self, args):
        acc = DataHub.account_info
        positions = list(DataHub.positions.values())

        if not acc:
            await self.notifier.send_message("⚠️ 帳戶數據尚未就緒")
            return

        # 持倉明細
        pos_lines = ""
        total_unrealized = 0.0
        for p in positions:
            side_str = "多 🟢" if p.side == 0 else "空 🔴"
            pos_lines += (
                f"\n  `{p.symbolName}` {side_str} {p.size}口\n"
                f"  均價：`{p.averagePrice:.2f}` 現價：`{p.currentPrice:.2f}`\n"
                f"  浮盈：`${p.unrealized_pnl_usd:+.2f}`"
            )
            total_unrealized += p.unrealized_pnl_usd

        strategy_status = "⏸ 已暫停" if getattr(self.engine, 'strategy_paused', False) else "▶ 運行中"

        await self.notifier.send_message(
            f"📊 *系統狀態*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"帳戶：`{acc.name}`\n"
            f"餘額：`${acc.balance:,.2f}`\n"
            f"已實現：`${acc.realized_pnl:+.2f}`\n"
            f"總浮盈：`${total_unrealized:+.2f}`\n"
            f"策略：{strategy_status}\n"
            f"時間：`{datetime.utcnow().strftime('%H:%M:%S')} UTC`"
            + (f"\n━━━━━━━━━━━━━━━\n*持倉明細：*{pos_lines}" if pos_lines else "\n持倉：無")
        )

    async def _cmd_risk(self, args):
        rm = self.engine.risk_manager
        acc = DataHub.account_info

        if not acc:
            await self.notifier.send_message("⚠️ 帳戶數據尚未就緒")
            return

        total_pnl = acc.realized_pnl + sum(
            p.unrealized_pnl_usd for p in DataHub.positions.values()
        )

        await self.notifier.send_message(
            f"🛡️ *風控狀態*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"今日損益：`${total_pnl:+.2f}` / 上限：`-${rm.max_daily_loss:.0f}`\n"
            f"今日交易：`{rm.daily_trades}` / 上限：`{rm.max_daily_trades}`\n"
            f"連續虧損：`{rm.consecutive_losses}` / 上限：`{rm.max_consec_losses}`\n"
            f"帳戶餘額：`${acc.balance:,.2f}` / 保護線：`${rm.min_balance:,.0f}`\n"
            f"交易時段：{'✅ 開放' if rm.check_safety()[0] else '❌ 關閉'}"
        )

    async def _cmd_pause(self, args):
        self.engine.strategy_paused = True
        logger.info("[Bot] 策略已暫停（人工指令）")
        await self.notifier.send_message(
            "⏸ *策略已暫停*\n"
            "現有持倉繼續持有，不會下新單。\n"
            "輸入 /resume 恢復。"
        )

    async def _cmd_resume(self, args):
        self.engine.strategy_paused = False
        logger.info("[Bot] 策略已恢復（人工指令）")
        await self.notifier.send_message("▶ *策略已恢復*\n系統繼續正常下單。")

    async def _cmd_stop(self, args):
        logger.warning("[Bot] 收到停止指令，策略主循環將關閉")
        self.engine.running = False
        await self.notifier.send_message(
            "🛑 *策略已停止*\n"
            "主循環關閉，現有持倉不受影響。\n"
            "⚠️ 需要手動重啟系統。"
        )

    async def _cmd_close(self, args):
        positions = list(DataHub.positions.values())
        account_id = self.engine.config["account_id"]

        if not positions:
            await self.notifier.send_message("ℹ️ 目前沒有任何持倉")
            return

        await self.notifier.send_message(
            f"⚡ *執行平倉指令*\n"
            f"共 {len(positions)} 個持倉，平倉中..."
        )

        success_count = 0
        for pos in positions:
            ok = await self.engine.trading_service.close_position(account_id, pos.contractId)
            if ok:
                success_count += 1
                logger.info(f"[Bot] 平倉成功: {pos.contractId}")
            else:
                logger.error(f"[Bot] 平倉失敗: {pos.contractId}")

        await self.notifier.send_message(
            f"{'✅' if success_count == len(positions) else '⚠️'} *平倉完成*\n"
            f"成功：{success_count} / {len(positions)} 個持倉"
        )
    async def _cmd_diag(self, args):
        """診斷：顯示 DataHub 原始數據狀態"""
        from core.data_hub import DataHub
        acc = DataHub.account_info
        bars = DataHub.bars

        lines = ["🔍 *診斷報告*\n━━━━━━━━━━━━━━━"]

        # 帳戶原始欄位
        if acc:
            lines.append(f"帳戶餘額: `{acc.balance}`")
            lines.append(f"已實現PnL: `{acc.realized_pnl}`")
        else:
            lines.append("⚠️ DataHub.account_info = None")

        # K 棒狀態
        if bars:
            for symbol, df in bars.items():
                lines.append(f"K棒 `{symbol}`: {len(df)} 根 | 最新: `{df.index[-1] if not df.empty else '無'}`")
        else:
            lines.append("⚠️ DataHub.bars = 空")

        # 策略 dashboard
        strategy = getattr(self.engine, '_strategy', None)
        if strategy:
            d = getattr(strategy, 'dashboard', {})
            lines.append(f"策略ATR: `{d.get('atr', 'N/A')}`")
            lines.append(f"SR區域數: `{len(d.get('sr_zones', []))}`")
        else:
            lines.append("⚠️ 找不到 strategy 引用")

        await self.notifier.send_message("\n".join(lines))
