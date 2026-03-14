# main.py
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
from services.bar_store import BarStore
from services.dashboard import Dashboard

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class ProjectXEngine:
    def __init__(self):
        self.running = True
        self.strategy_paused = False  # Bot /pause 和 /resume 控制

        # 核心服務
        self.client          = TopstepXClient()
        self.market_service  = MarketDataService(self.client)
        self.notifier        = TelegramService()
        self.trading_service = TradingService(self.client, notifier=self.notifier)
        self.order_monitor   = OrderMonitor(self.trading_service, self.market_service, notifier=self.notifier)
        self.risk_manager    = RiskManager(notifier=self.notifier)
        self.account_service = AccountService(self.client)
        self.event_engine    = EventEngine()
        self.daily_report    = DailyReportService(self.trading_service, self.notifier)

        # Bot 指令處理器（注入 engine 自身）
        self.bot_handler = BotCommandHandler(self)
        self.notifier.set_command_handler(self.bot_handler)

        self.config = {
            "account_id":      int(os.getenv("ACCOUNT_ID", "18699057")),  # FIX #8: 從 .env 讀取
            "symbol":          "CON.F.US.MNQ.H26",
            "contract_expiry": "2026-03-21",   # 合約到期日，換月時更新
            "side":            0,
            "size":            int(os.getenv("TRADE_SIZE", "1")),
            "sl_points":       20,
            "tp_points":       40,
            "ticks_per_point": 4,
            "cooldown":        10
        }

    # ──────────────────────────────────────────
    # 啟動
    # ──────────────────────────────────────────
    async def initialize(self) -> bool:
        logger.info("🎬 ProjectX 引擎啟動中...")
        success = await self.client.login()
        if not success:
            await self.notifier.send_message("🚨 *緊急：TopstepX 登入失敗，系統無法啟動！*")
        return success

    async def _wait_for_sync(self, timeout: int = 30):
        """
        等待 OrderMonitor 完成第一次帳戶同步。
        確保策略啟動前已知道是否有現有持倉，防止重複進場。
        """
        logger.info("⏳ 等待帳戶數據同步...")
        for _ in range(timeout):
            if DataHub.account_info is not None:
                logger.info(f"✅ 數據同步完成 | 帳戶: {DataHub.account_info.name}")
                return True
            await asyncio.sleep(1)
        raise RuntimeError("帳戶數據同步超時（30秒），請檢查網路或 API 狀態")

    # ──────────────────────────────────────────
    # 主循環
    # ──────────────────────────────────────────
    async def run(self):
        if not await self.initialize():
            logger.error("❌ 登入失敗，系統退出")
            return

        # 啟動並行 Task
        tasks = [
            asyncio.create_task(self.market_service.start_polling(self.config["symbol"])),
            asyncio.create_task(self.order_monitor.start(self.config["account_id"])),
            asyncio.create_task(self.event_engine.start()),
            asyncio.create_task(self.client.start_token_refresh()),
            asyncio.create_task(self.notifier.start_receiving()),   # Bot 指令接收
        ]

        # 初始化歷史數據存檔（補拉缺口後注入 market_service）
        self.bar_store = BarStore(market_service=self.market_service)
        await self.bar_store.initialize(self.config["symbol"])
        self.market_service.bar_store = self.bar_store
        self.event_engine.bar_store = self.bar_store   # 讓 EventEngine 用完整歷史觸發 ON_BAR
        logger.info(self.bar_store.summary(self.config["symbol"]))

        # 等帳戶數據同步後再讓策略開始
        await self._wait_for_sync()
        
        #from strategies.smc_strategy import SMCStrategy
        #strategy = SMCStrategy(
        #    trading_service=self.trading_service,
        #    market_service=self.market_service,
        #    notifier=self.notifier,
        #    ob_lookback=20,
        #    atr_period=14,
        #    atr_sl_mult=1.5,
        #    atr_tp_mult=3.0,
        #    size=1
        #)
        #strategy.account_id = self.config["account_id"]
        #strategy.contract_id = self.config["symbol"]
        #self.event_engine.register("ON_BAR", strategy.on_bar)
        from strategies.snr_strategy_v3 import SNRStrategyV3

        strategy = SNRStrategyV3(
            trading_service = self.trading_service,
            market_service  = self.market_service,
            notifier        = self.notifier,
            engine          = self,
            contract_id     = self.config["symbol"],
            # ── 定案參數（v3c Walk-Forward 驗證通過）──────────────
            fake_break_depth_mult = 0.60,
            htf_primary_ema       = 50,
            htf_secondary_ema     = 9,
            min_rr                = 3.0,
            sl_max_atr_mult       = 2.0,
            sl_buffer_mult        = 0.15,
            sr_sensitivity        = 1.5,
            sr_near_mult          = 2.5,
            vol_mult              = 0.0,   # 停用量能過濾
            use_fixed_rr          = True,  # 固定 RR 止盈（不找對面 SR）
            # ── 時段：NY only 13:30~20:00 UTC ─────────────────────
            session_london_start  = 99,    # 99 = 停用倫敦時段
            session_london_end    = 99,
            session_ny_start      = 13,
            session_ny_start_min  = 30,
            session_ny_end        = 20,
            # ── 口數 ──────────────────────────────────────────────
            # size 從 engine.config["size"] 讀取（在 _place_order 內自動取得）
        )
        strategy.account_id = self.config["account_id"]
        self._strategy = strategy  # 供 /diag 指令讀取
        self.event_engine.register("ON_BAR", strategy.on_bar)

        # 啟動 console 儀表板
        self.dashboard = Dashboard(engine=self, strategy=strategy, refresh_secs=5.0)
        tasks.append(asyncio.create_task(self.dashboard.start()))

        # 測試腳本（全部通過後移除這兩行）
        #test_task = asyncio.create_task(self._run_unattended_test())
        #tasks.append(test_task)


        # 通知系統啟動
        acc = DataHub.account_info
        await self.notifier.notify_system_start(
            acc.name if acc else "未知", self.config["symbol"]
        )
        logger.info("🚀 系統已進入並行運作模式")

        _last_balance  = 0.0
        _last_pnl      = 0.0
        _last_pos_size = -1

        try:
            while self.running:
                acc = DataHub.account_info
                pos = DataHub.positions.get(self.config["symbol"])
                pos_size = pos.size if pos else 0

                if acc:
                    if abs(acc.balance - _last_balance) > 0.01 or abs(acc.realized_pnl - _last_pnl) > 0.01:
                        logger.info(
                            f"📊 帳戶: {acc.name} | "
                            f"餘額: ${acc.balance:,.2f} | "
                            f"已實現: ${acc.realized_pnl:+.2f}"
                        )
                        _last_balance = acc.balance
                        _last_pnl     = acc.realized_pnl

                if pos_size != _last_pos_size:
                    if pos_size > 0:
                        logger.info(f"📦 持倉中 | 數量: {pos_size} | 浮盈: ${pos.unrealized_pnl_usd:+.2f}")
                    elif _last_pos_size > 0:
                        logger.info("📭 持倉已清空")
                    _last_pos_size = pos_size

                # 強制平倉檢查
                await self._check_force_close()

                # 合約到期提醒
                await self._check_contract_expiry()

                # 每日報表
                await self.daily_report.check_and_send(self.config["account_id"])

                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"💥 引擎異常: {e}")
            await self.notifier.send_message(f"💥 *系統異常*\n錯誤：{e}")
        finally:
            await self.shutdown(tasks)

    # ──────────────────────────────────────────
    # 輔助功能
    # ──────────────────────────────────────────
    async def _check_force_close(self):
        """到達強制平倉時間時，平掉所有持倉"""
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
        """合約到期前 3 天發 Telegram 提醒（每天只發一次，重啟後不重發）"""
        expiry_str = self.config.get("contract_expiry", "")
        if not expiry_str:
            return
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
            days_left = (expiry - datetime.utcnow()).days
            if 0 <= days_left <= 3:
                now_date = datetime.utcnow().strftime("%Y-%m-%d")
                # FIX #9: 改用 StateStore 持久化，重啟後不會重複發通知
                from core.state_store import StateStore
                store = StateStore()
                state = store.get_strategy_state("expiry_notified")
                if state.get("date") != now_date:
                    store.update_strategy_state("expiry_notified", {"date": now_date})
                    await self.notifier.send_message(
                        f"⚠️ *合約即將到期*\n"
                        f"`{self.config['symbol']}` 還有 *{days_left} 天* 到期\n"
                        f"請手動換月並更新 config！"
                    )
                    logger.warning(f"[合約] {self.config['symbol']} 還有 {days_left} 天到期")
        except ValueError:
            pass

    async def shutdown(self, tasks):
        self.running = False
        if hasattr(self, "dashboard"):
            self.dashboard.stop()
        self.market_service.stop_polling()
        self.event_engine.stop()
        self.order_monitor.stop()
        for t in tasks:
            t.cancel()
        await self.client.http_client.aclose()
        await self.notifier.notify_system_stop()
        logger.info("🛑 系統已關閉")

    async def _run_unattended_test(self):
        from strategies.unattended_test import UnattendedTestStrategy
        t = UnattendedTestStrategy(self)
        await t.run_all()

# ──────────────────────────────────────────
# 最外層：自動重啟循環
# ──────────────────────────────────────────
if __name__ == "__main__":
    while True:
        try:
            if sys.platform == 'win32':
                # Windows 每次重啟都要建立新的 event loop，否則 asyncio.run 會找不到 loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(ProjectXEngine().run())
                loop.close()
            else:
                asyncio.run(ProjectXEngine().run())
            logger.info("系統正常退出")
            break  # 正常退出（KeyboardInterrupt 以外）不重啟
        except KeyboardInterrupt:
            logger.info("使用者手動停止")
            break
        except Exception as e:
            logger.error(f"💥 系統崩潰: {e}，5 秒後自動重啟...")
            time.sleep(5)
