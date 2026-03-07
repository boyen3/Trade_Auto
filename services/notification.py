# services/notification.py
import httpx
import asyncio
import os
from datetime import datetime, timezone
from utils.logger import logger


class TelegramService:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id = 0
        self._command_handler = None
        self._enabled = bool(self.token and self.chat_id)

        if not self._enabled:
            logger.warning("⚠️ Telegram 設定缺失，通知與 Bot 功能停用")

    # ──────────────────────────────────────────
    # 發送
    # ──────────────────────────────────────────
    async def send_message(self, text: str, retries: int = 3):
        if not self._enabled:
            return
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(f"{self.api_base}/sendMessage", json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown"
                    })
                    if resp.status_code == 200:
                        return
                    logger.error(f"❌ Telegram 發送失敗 ({resp.status_code}): {resp.text}")
                    return
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"❌ Telegram 連線異常: {e}")
                else:
                    await asyncio.sleep(2 * (attempt + 1))

    # ──────────────────────────────────────────
    # Bot 指令接收
    # ──────────────────────────────────────────
    def set_command_handler(self, handler):
        """由 BotCommandHandler 注入"""
        self._command_handler = handler

    async def start_receiving(self):
        """
        背景 Task：每 2 秒輪詢 getUpdates。
        只接受來自自己 chat_id 的指令，其他人一律忽略。
        """
        if not self._enabled:
            logger.warning("⚠️ Telegram 未設定，Bot 指令接收停用")
            return

        logger.info("🤖 Telegram Bot 指令接收已啟動")

        # 清空離線期間累積的舊指令（避免重啟後被舊 /stop 打掉）
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{self.api_base}/getUpdates", json={
                    "offset": -1,
                    "allowed_updates": ["message"]
                })
                if resp.status_code == 200:
                    results = resp.json().get("result", [])
                    if results:
                        self._last_update_id = results[-1]["update_id"]
                        logger.info(f"[Bot] 已清空 {len(results)} 條舊指令")
        except Exception as e:
            logger.warning(f"[Bot] 清空舊指令失敗（不影響運作）: {e}")

        while True:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(f"{self.api_base}/getUpdates", json={
                        "offset": self._last_update_id + 1,
                        "timeout": 10,
                        "allowed_updates": ["message"]
                    })
                    if resp.status_code == 200:
                        for update in resp.json().get("result", []):
                            self._last_update_id = update["update_id"]
                            await self._process_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ Telegram 接收異常: {e}")
                await asyncio.sleep(5)
            await asyncio.sleep(2)

    async def _process_update(self, update: dict):
        message = update.get("message", {})
        if not message:
            return

        # 安全驗證：只接受自己的 chat_id
        sender_id = str(message.get("chat", {}).get("id", ""))
        if sender_id != str(self.chat_id):
            logger.warning(f"⚠️ 拒絕未授權指令 (chat_id: {sender_id})")
            return

        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return

        parts = text.split()
        command = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []
        logger.info(f"[Bot] 收到指令: {command} {args}")

        if self._command_handler:
            # 傳入訊息時間戳，讓 BotCommandHandler 過濾啟動前的舊指令
            msg_date = datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc)
            await self._command_handler.handle(command, args, message_date=msg_date)
        else:
            await self.send_message("⚠️ 指令處理器尚未就緒")

    # ──────────────────────────────────────────
    # 通知方法
    # ──────────────────────────────────────────
    async def send_trade_notification(self, strategy_name, action, symbol, price, size):
        emoji = "🟢" if "BUY" in action.upper() else "🔴"
        await self.send_message(
            f"{emoji} *交易執行通知*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🔹 *策略*: {strategy_name}\n"
            f"🔹 *動作*: {action}\n"
            f"🔹 *標的*: {symbol}\n"
            f"🔹 *價格*: {price}\n"
            f"🔹 *口數*: {size}\n"
            f"━━━━━━━━━━━━━━━"
        )

    async def notify_system_start(self, account_name: str, symbol: str):
        await self.send_message(
            f"🚀 *系統啟動*\n"
            f"帳戶：`{account_name}`\n"
            f"商品：`{symbol}`\n"
            f"時間：`{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
            f"可用指令：/help"
        )

    async def notify_system_stop(self, reason: str = "正常關閉"):
        await self.send_message(f"🛑 *系統已關閉*\n原因：{reason}")

    async def notify_order_placed(self, contract_id, side, size, order_id, sl_ticks, tp_ticks):
        side_str = "買入 🟢" if side == 0 else "賣出 🔴"
        await self.send_message(
            f"📋 *下單通知*\n"
            f"商品：`{contract_id}`\n"
            f"方向：{side_str} `{size}口`\n"
            f"訂單：`#{order_id}`\n"
            f"SL：`{sl_ticks} ticks` ｜ TP：`{tp_ticks} ticks`"
        )

    async def notify_order_filled(self, contract_id, side, size, order_id, price):
        side_str = "買入 🟢" if side == 0 else "賣出 🔴"
        await self.send_message(
            f"✅ *成交通知*\n"
            f"商品：`{contract_id}`\n"
            f"方向：{side_str} `{size}口`\n"
            f"成交價：`{price}`\n"
            f"訂單：`#{order_id}`"
        )

    async def notify_order_cancelled(self, order_id, contract_id, reason=""):
        await self.send_message(
            f"❌ *撤單通知*\n"
            f"訂單：`#{order_id}`\n"
            f"商品：`{contract_id}`"
            + (f"\n原因：{reason}" if reason else "")
        )

    async def notify_risk_triggered(self, reason: str, total_pnl: float, limit: float):
        await self.send_message(
            f"🚨 *緊急：風控觸發*\n"
            f"原因：{reason}\n"
            f"當前損益：`${total_pnl:+.2f}`\n"
            f"限制：`${limit:.2f}`"
        )

    async def notify_account_summary(self, account_name, balance, realized_pnl, positions):
        pos_lines = ""
        for p in positions:
            side_str = "多" if p.side == 0 else "空"
            pos_lines += f"\n  {p.symbolName} {side_str}{p.size}口 浮盈:`${p.unrealized_pnl_usd:+.2f}`"
        await self.send_message(
            f"💓 *系統心跳 | {datetime.utcnow().strftime('%H:%M')} UTC*\n"
            f"帳戶：`{account_name}`\n"
            f"餘額：`${balance:,.2f}`\n"
            f"已實現：`${realized_pnl:+.2f}`"
            + (f"\n持倉：{pos_lines}" if pos_lines else "\n持倉：無")
        )

    async def notify_api_error(self, endpoint: str, error_msg: str):
        await self.send_message(
            f"⚠️ *API 連線異常*\n"
            f"端點：`{endpoint}`\n"
            f"錯誤：{error_msg}"
        )
