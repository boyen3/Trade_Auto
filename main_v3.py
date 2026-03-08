# main.py（更新版：接入 SNR v3 5M 策略）
import asyncio
import sys
import os
import time
from datetime import datetime
from utils.logger import logger
from core.client import TopstepXClient
from core.data_hub import DataHub
from core.event_engine import EventEngine
from services.trading import TradingService
from services.market_data import MarketDataService
from services.order_monitor import OrderMonitor
from services.notification import TelegramService
from services.risk_manager import RiskManager
from services.account import AccountService
from services.daily_report import DailyReportService
from services.bot_handler import BotCommandHandler

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class ProjectXEngine:
    def __init__(self):
        self.running = True
        self.strategy_paused = False

        self.client          = TopstepXClient()
        self.market_service  = MarketDataService(self.client)
        self.notifier        = TelegramService()
        self.trading_service = TradingService(self.client, notifier=self.notifier)
        self.order_monitor   = OrderMonitor(self.trading_service, self.market_service, notifier=self.notifier)
        self.risk_manager    = RiskManager(notifier=self.notifier)
        self.account_service = AccountService(self.client)
        self.event_engine    = EventEngine()
        self.daily_report    = DailyReportService(self.trading_service, self.notifier)
        self.bot_handler     = BotCommandHandler(self)
        self.notifier.set_command_handler(self.bot_handler)

        # ── LiveBarStore（即時模式的多時框數據源）──────────────────
        from core.live_bar_store import LiveBarStore
        self.bar_store = LiveBarStore(self.market_service, cache_seconds=60)

        self.config = {
            "account_id":      18699057,
            "symbol":          "CON.F.US.MNQ.H26",
            "contract_expiry": "2026-03-21",
        }

    async def initialize(self) -> bool:
        logger.info("🎬 ProjectX 引擎啟動中...")
        success = await self.client.login()
        if not success:
            await self.notifier.send_message("🚨 *緊急：TopstepX 登入失敗，系統無法啟動！*")
        return success

    async def _wait_for_sync(self, timeout: int = 30):
        logger.info("⏳ 等待帳戶數據同步...")
        for _ in range(timeout):
            if DataHub.account_info is not None:
                logger.info(f"✅ 數據同步完成 | 帳戶: {DataHub.account_info.name}")
                return True
            await asyncio.sleep(1)
        raise RuntimeError("帳戶數據同步超時（30秒）")

    async def run(self):
        if not await self.initialize():
            logger.error("❌ 登入失敗，系統退出")
            return

        tasks = [
            asyncio.create_task(self.market_service.start_polling(self.config["symbol"])),
            asyncio.create_task(self.order_monitor.start(self.config["account_id"])),
            asyncio.create_task(self.event_engine.start()),
            asyncio.create_task(self.client.start_token_refresh()),
            asyncio.create_task(self.notifier.start_receiving()),
        ]

        await self._wait_for_sync()

        # ── SNR v3 5M 策略（C 高頻）──────────────────────────────
        from strategies.snr_strategy_v3 import SNRStrategyV3

        strategy = SNRStrategyV3(
            trading_service = self.trading_service,
            market_service  = self.market_service,
            notifier        = self.notifier,
            engine          = self,
            # C 高頻參數
            fake_break_depth_mult = 0.40,
            htf_primary_ema       = 200,
            htf_secondary_ema     = 9,
            min_rr                = 1.2,
            vol_mult              = 0.0,
            sl_max_atr_mult       = 1.5,
            # London + NY 時段
            session_london_start  = 7,
            session_london_end    = 12,
            session_ny_start      = 13,
            session_ny_start_min  = 30,
            session_ny_end        = 20,
            # 口數（模擬先用 1 口，正式考核改為 6-7 口）
            size                  = 1,
        )
        strategy.account_id  = self.config["account_id"]
        strategy.contract_id = self.config["symbol"]
        self.event_engine.register("ON_BAR", strategy.on_bar)

        acc = DataHub.account_info
        await self.notifier.notify_system_start(
            acc.name if acc else "未知", self.config["symbol"]
        )
        logger.info("🚀 SNR v3 5M C高頻策略已啟動")

        try:
            while self.running:
                acc = DataHub.account_info
                pos = DataHub.positions.get(self.config["symbol"])
                pos_size = pos.size if pos else 0

                if acc:
                    logger.info(
                        f"📊 帳戶: {acc.name} | "
                        f"餘額: ${acc.balance:,.2f} | "
                        f"已實現: ${acc.realized_pnl:+.2f}"
                    )
                if pos_size > 0:
                    logger.info(f"📦 持倉中 | 數量: {pos_size} | 浮盈: ${pos.unrealized_pnl_usd:+.2f}")

                await self._check_force_close()
                await self._check_contract_expiry()
                await self.daily_report.check_and_send(self.config["account_id"])
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"💥 引擎異常: {e}")
            await self.notifier.send_message(f"💥 *系統異常*\n錯誤：{e}")
        finally:
            await self.shutdown(tasks)

    async def _check_force_close(self):
        if not self.risk_manager.is_force_close_time():
            return
        positions = list(DataHub.positions.values())
        if not positions:
            return
        logger.warning("⚠️ 收盤強制平倉觸發")
        await self.notifier.send_message("⚠️ *收盤強制平倉執行中...*")
        for pos in positions:
            await self.trading_service.close_position(
                self.config["account_id"], pos.contractId
            )

    async def _check_contract_expiry(self):
        expiry_str = self.config.get("contract_expiry", "")
        if not expiry_str:
            return
        try:
            expiry    = datetime.strptime(expiry_str, "%Y-%m-%d")
            days_left = (expiry - datetime.utcnow()).days
            if 0 <= days_left <= 3:
                now_date  = datetime.utcnow().strftime("%Y-%m-%d")
                cache_key = f"_expiry_notified_{now_date}"
                if not getattr(self, cache_key, False):
                    setattr(self, cache_key, True)
                    await self.notifier.send_message(
                        f"⚠️ *合約即將到期*\n"
                        f"`{self.config['symbol']}` 還有 *{days_left} 天* 到期\n"
                        f"請手動換月並更新 config！"
                    )
        except ValueError:
            pass

    async def shutdown(self, tasks):
        self.running = False
        self.market_service.stop_polling()
        self.event_engine.stop()
        self.order_monitor.stop()
        for t in tasks:
            t.cancel()
        await self.client.http_client.aclose()
        await self.notifier.notify_system_stop()
        logger.info("🛑 系統已關閉")


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(ProjectXEngine().run())
            logger.info("系統正常退出")
            break
        except KeyboardInterrupt:
            logger.info("使用者手動停止")
            break
        except Exception as e:
            logger.error(f"💥 系統崩潰: {e}，5 秒後自動重啟...")
            time.sleep(5)
