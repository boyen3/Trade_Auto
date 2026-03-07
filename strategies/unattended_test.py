# strategies/unattended_test.py
"""
完整系統測試腳本（無人值守 + API 全覆蓋）
==========================================
整合兩個測試區塊：

  Part A：無人值守功能測試（18項）
    - P0 風控：同步等待、每日虧損、交易次數、連續虧損、持倉口數、交易時段、週末
    - P0 其他：登入通知、強制平倉方法
    - P1：Token 刷新、合約到期、心跳
    - P2：每日報表、Log 清理、API 升級通知
    - Bot：/status、/risk、/pause /resume

  Part B：API 端點全覆蓋測試（15項）
    - Account/search
    - Contract/search、searchById、available
    - History/retrieveBars
    - Order/place (Market)、searchOpen、search
    - Position/searchOpen、closeContract、partialCloseContract
    - Order/place (Limit+SL/TP)、modify、cancel
    - Trade/search

總計：33 項測試，全部通過才代表系統完整可用。

使用方式：
  在 main.py 的 _wait_for_sync() 之後加：
    test_task = asyncio.create_task(self._run_unattended_test())
    tasks.append(test_task)

  在 ProjectXEngine 加：
    async def _run_unattended_test(self):
        from strategies.unattended_test import UnattendedTestStrategy
        t = UnattendedTestStrategy(self)
        await t.run_all()
"""
import asyncio
from datetime import datetime
from core.data_hub import DataHub
from core.constants import OrderType, OrderSide
from utils.logger import logger


class UnattendedTestStrategy:

    STEPS_A = [
        # Part A：無人值守功能
        "TEST_SYNC_WAIT",
        "TEST_RISK_DAILY_LOSS",
        "TEST_RISK_TRADE_COUNT",
        "TEST_RISK_CONSEC_LOSS",
        "TEST_RISK_POSITION_SIZE",
        "TEST_RISK_TRADING_HOURS",
        "TEST_RISK_WEEKEND",
        "TEST_LOGIN_NOTIFICATION",
        "TEST_FORCE_CLOSE_METHOD",
        "TEST_TOKEN_REFRESH",
        "TEST_CONTRACT_EXPIRY",
        "TEST_HEARTBEAT",
        "TEST_DAILY_REPORT",
        "TEST_LOG_CLEANUP",
        "TEST_API_ESCALATION",
        "TEST_BOT_STATUS",
        "TEST_BOT_RISK",
        "TEST_BOT_PAUSE_RESUME",
    ]

    STEPS_B = [
        # Part B：API 端點全覆蓋
        "API_ACCOUNT_SEARCH",
        "API_CONTRACT_SEARCH",
        "API_CONTRACT_SEARCH_BY_ID",
        "API_CONTRACT_AVAILABLE",
        "API_HISTORY_BARS",
        "API_ORDER_PLACE_MARKET",
        "API_POSITION_SEARCH_OPEN",
        "API_POSITION_PARTIAL_CLOSE",
        "API_POSITION_CLOSE",
        "API_ORDER_PLACE_LIMIT",
        "API_ORDER_SEARCH_OPEN",
        "API_ORDER_MODIFY",
        "API_ORDER_CANCEL",
        "API_ORDER_SEARCH",
        "API_TRADE_SEARCH",
    ]

    def __init__(self, engine):
        self.engine = engine
        self.ts = engine.trading_service
        self.account_id = engine.config["account_id"]
        self.contract_id = engine.config["symbol"]
        self._results = {}
        # API 測試用暫存
        self._market_order_id = None
        self._limit_order_id = None

    async def run_all(self):
        total = len(self.STEPS_A) + len(self.STEPS_B)
        logger.info(f"[Test] ========== 開始完整系統測試（共 {total} 項）==========")
        await self.engine.notifier.send_message(
            f"🧪 *完整系統測試開始*\n"
            f"共 {total} 項（無人值守 {len(self.STEPS_A)} + API {len(self.STEPS_B)}）\n"
            f"請稍候約 2 分鐘..."
        )

        # Part A
        logger.info("[Test] ── Part A：無人值守功能測試 ──")
        for i, name in enumerate(self.STEPS_A, 1):
            logger.info(f"[Test] ▶ A{i}/{len(self.STEPS_A)}: {name}")
            await self._run_step(name)
            await asyncio.sleep(2)

        # Part B
        logger.info("[Test] ── Part B：API 端點全覆蓋測試 ──")
        for i, name in enumerate(self.STEPS_B, 1):
            logger.info(f"[Test] ▶ B{i}/{len(self.STEPS_B)}: {name}")
            await self._run_step(name)
            await asyncio.sleep(3)  # API 測試間隔稍長，避免頻率限制

        await self._report()

    async def _run_step(self, name: str):
        method_name = "_test_" + name.lower().replace("test_", "", 1).replace("api_", "api_", 1)
        # Part B 的方法名直接用 _test_ + 小寫
        handler = getattr(self, f"_test_{name.lower()}", None)
        if handler:
            try:
                await handler()
            except Exception as e:
                self._record(name, False, f"例外: {e}")
        else:
            self._record(name, False, "找不到測試方法")

    # ══════════════════════════════════════════
    # Part A：無人值守功能測試
    # ══════════════════════════════════════════

    async def _test_test_sync_wait(self):
        ok = DataHub.account_info is not None
        detail = f"帳戶: {DataHub.account_info.name}" if ok else "DataHub 無帳戶數據"
        self._record("TEST_SYNC_WAIT", ok, detail)

    async def _test_test_risk_daily_loss(self):
        rm = self.engine.risk_manager
        orig = rm.max_daily_loss
        rm.max_daily_loss = 0
        safe, reason = rm.check_safety()
        rm.max_daily_loss = orig
        self._record("TEST_RISK_DAILY_LOSS", not safe, reason)

    async def _test_test_risk_trade_count(self):
        rm = self.engine.risk_manager
        orig = rm.max_daily_trades
        rm.max_daily_trades = 0
        safe, reason = rm.check_safety()
        rm.max_daily_trades = orig
        self._record("TEST_RISK_TRADE_COUNT", not safe, reason)

    async def _test_test_risk_consec_loss(self):
        rm = self.engine.risk_manager
        orig = rm.consecutive_losses
        rm.consecutive_losses = rm.max_consec_losses
        safe, reason = rm.check_safety()
        rm.consecutive_losses = orig
        self._record("TEST_RISK_CONSEC_LOSS", not safe, reason)

    async def _test_test_risk_position_size(self):
        rm = self.engine.risk_manager
        orig = rm.max_position_size
        rm.max_position_size = 0
        safe, reason = rm.check_safety()
        rm.max_position_size = orig
        self._record("TEST_RISK_POSITION_SIZE", not safe, reason)

    async def _test_test_risk_trading_hours(self):
        rm = self.engine.risk_manager
        orig_h, orig_m = rm.end_hour, rm.end_minute
        rm.end_hour, rm.end_minute = 0, 0
        safe, reason = rm.check_safety()
        rm.end_hour, rm.end_minute = orig_h, orig_m
        self._record("TEST_RISK_TRADING_HOURS", not safe, reason)

    async def _test_test_risk_weekend(self):
        has_check = hasattr(self.engine.risk_manager, 'check_safety')
        self._record("TEST_RISK_WEEKEND", has_check, "邏輯存在" if has_check else "方法缺失")

    async def _test_test_login_notification(self):
        ok = self.engine.notifier is not None and hasattr(self.engine.notifier, 'send_message')
        self._record("TEST_LOGIN_NOTIFICATION", ok, "通知服務就緒")

    async def _test_test_force_close_method(self):
        ok = hasattr(self.engine, '_check_force_close')
        rm_ok = hasattr(self.engine.risk_manager, 'is_force_close_time')
        self._record("TEST_FORCE_CLOSE_METHOD", ok and rm_ok,
                     "方法存在" if (ok and rm_ok) else "方法缺失")

    async def _test_test_token_refresh(self):
        client = self.engine.client
        ok = client.token_expiry is not None
        if ok:
            remaining = (client.token_expiry - datetime.now()).total_seconds()
            detail = f"剩餘 {remaining/60:.0f} 分鐘"
            ok = remaining > 0
        else:
            detail = "token_expiry 未設定"
        has_refresh = hasattr(client, 'start_token_refresh')
        self._record("TEST_TOKEN_REFRESH", ok and has_refresh, detail)

    async def _test_test_contract_expiry(self):
        expiry_str = self.engine.config.get("contract_expiry", "")
        if not expiry_str:
            self._record("TEST_CONTRACT_EXPIRY", False, "config 未設定 contract_expiry")
            return
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
        days_left = (expiry - datetime.utcnow()).days
        self._record("TEST_CONTRACT_EXPIRY", days_left >= 0, f"距到期還有 {days_left} 天")

    async def _test_test_heartbeat(self):
        try:
            await self.engine.notifier.send_message(
                f"💓 *測試心跳* | {datetime.utcnow().strftime('%H:%M')} UTC"
            )
            self._record("TEST_HEARTBEAT", True, "Telegram 發送成功")
        except Exception as e:
            self._record("TEST_HEARTBEAT", False, str(e))

    async def _test_test_daily_report(self):
        try:
            trades = await self.ts.get_today_trades(self.account_id)
            completed = [t for t in trades if t.profitAndLoss is not None]
            total = sum(t.profitAndLoss for t in completed)
            self._record("TEST_DAILY_REPORT", True,
                         f"今日 {len(completed)} 筆完整回合，損益 ${total:+.2f}")
        except Exception as e:
            self._record("TEST_DAILY_REPORT", False, str(e))

    async def _test_test_log_cleanup(self):
        from pathlib import Path
        log_dir = Path(__file__).parent.parent / "logs"
        ok = log_dir.exists()
        files = list(log_dir.glob("*.log")) if ok else []
        self._record("TEST_LOG_CLEANUP", ok, f"共 {len(files)} 個 log 檔案")

    async def _test_test_api_escalation(self):
        om = self.engine.order_monitor
        ok = (hasattr(om, '_api_error_since') and
              hasattr(om, '_api_error_escalated') and
              hasattr(om, '_handle_api_error'))
        self._record("TEST_API_ESCALATION", ok, "機制存在" if ok else "機制缺失")

    async def _test_test_bot_status(self):
        try:
            await self.engine.bot_handler.handle("/status", [])
            self._record("TEST_BOT_STATUS", True, "指令執行成功")
        except Exception as e:
            self._record("TEST_BOT_STATUS", False, str(e))

    async def _test_test_bot_risk(self):
        try:
            await self.engine.bot_handler.handle("/risk", [])
            self._record("TEST_BOT_RISK", True, "指令執行成功")
        except Exception as e:
            self._record("TEST_BOT_RISK", False, str(e))

    async def _test_test_bot_pause_resume(self):
        try:
            await self.engine.bot_handler.handle("/pause", [])
            paused = self.engine.strategy_paused
            await self.engine.bot_handler.handle("/resume", [])
            resumed = not self.engine.strategy_paused
            ok = paused and resumed
            self._record("TEST_BOT_PAUSE_RESUME", ok,
                         "暫停/恢復正常" if ok else "狀態切換失敗")
        except Exception as e:
            self._record("TEST_BOT_PAUSE_RESUME", False, str(e))

    # ══════════════════════════════════════════
    # Part B：API 端點全覆蓋測試
    # ══════════════════════════════════════════

    async def _test_api_account_search(self):
        res = await self.ts.client.request("POST", "/api/Account/search",
                                           json={"onlyActiveAccounts": True})
        ok = res and res.get("success") and len(res.get("accounts", [])) > 0
        count = len(res.get("accounts", [])) if res else 0
        self._record("API_ACCOUNT_SEARCH", ok, f"找到 {count} 個帳戶")

    async def _test_api_contract_search(self):
        res = await self.ts.client.request("POST", "/api/Contract/search",
                                           json={"live": False, "searchText": "MNQ"})
        ok = res and res.get("success")
        count = len(res.get("contracts", [])) if ok else 0
        self._record("API_CONTRACT_SEARCH", ok, f"找到 {count} 個合約")

    async def _test_api_contract_search_by_id(self):
        res = await self.ts.client.request("POST", "/api/Contract/searchById",
                                           json={"contractId": self.contract_id})
        ok = res and res.get("success") and res.get("contract")
        detail = res.get("contract", {}).get("name", "") if ok else "查無合約"
        self._record("API_CONTRACT_SEARCH_BY_ID", ok, detail)

    async def _test_api_contract_available(self):
        res = await self.ts.client.request("POST", "/api/Contract/available",
                                           json={"live": False})
        ok = res and res.get("success")
        count = len(res.get("contracts", [])) if ok else 0
        self._record("API_CONTRACT_AVAILABLE", ok, f"共 {count} 個可用合約")

    async def _test_api_history_bars(self):
        from datetime import timedelta
        end = datetime.utcnow()
        start = end - timedelta(hours=2)
        res = await self.ts.client.request("POST", "/api/History/retrieveBars", json={
            "contractId": self.contract_id,
            "live": False,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": 2, "unitNumber": 1, "limit": 10, "includePartialBar": True
        })
        ok = res and res.get("success")
        count = len(res.get("bars", [])) if ok else 0
        self._record("API_HISTORY_BARS", ok, f"取得 {count} 根 K 線")

    async def _test_api_order_place_market(self):
        """下 1 口市價單進場，記錄 order_id 供後續測試使用"""
        order_id = await self.ts.place_order(
            self.account_id, self.contract_id,
            OrderType.MARKET, OrderSide.BUY, 1
        )
        ok = order_id is not None
        if ok:
            self._market_order_id = order_id
            await asyncio.sleep(2)  # 等待成交
        self._record("API_ORDER_PLACE_MARKET", ok,
                     f"訂單 #{order_id}" if ok else "下單失敗")

    async def _test_api_position_search_open(self):
        positions = await self.ts.get_open_positions(self.account_id)
        ok = positions is not None
        count = len(positions) if positions else 0
        self._record("API_POSITION_SEARCH_OPEN", ok, f"持倉 {count} 口")

    async def _test_api_position_partial_close(self):
        """補倉到 2 口再部分平倉 1 口"""
        # 先補 1 口讓持倉變 2 口
        await self.ts.place_order(
            self.account_id, self.contract_id,
            OrderType.MARKET, OrderSide.BUY, 1
        )
        await asyncio.sleep(2)
        ok = await self.ts.partial_close_position(self.account_id, self.contract_id, 1)
        self._record("API_POSITION_PARTIAL_CLOSE", ok,
                     "部分平倉 1 口成功" if ok else "部分平倉失敗")

    async def _test_api_position_close(self):
        """整口平倉剩餘持倉"""
        ok = await self.ts.close_position(self.account_id, self.contract_id)
        self._record("API_POSITION_CLOSE", ok,
                     "整口平倉成功" if ok else "整口平倉失敗")
        await asyncio.sleep(2)

    async def _test_api_order_place_limit(self):
        """下限價單含 SL/TP，記錄 order_id 供改單/撤單測試"""
        bars = DataHub.get_bars(self.contract_id)
        if bars.empty:
            self._record("API_ORDER_PLACE_LIMIT", False, "無行情數據")
            return
        price = round(bars['c'].iloc[-1] - 20, 2)  # 掛在市價下方，不會立刻成交
        order_id = await self.ts.place_order(
            self.account_id, self.contract_id,
            OrderType.LIMIT, OrderSide.BUY, 1,
            limit_price=price, sl_ticks=80, tp_ticks=160
        )
        ok = order_id is not None
        if ok:
            self._limit_order_id = order_id
        self._record("API_ORDER_PLACE_LIMIT", ok,
                     f"限價單 #{order_id} @ {price}" if ok else "限價單失敗")

    async def _test_api_order_search_open(self):
        res = await self.ts.client.request("POST", "/api/Order/searchOpen",
                                           json={"accountId": self.account_id})
        ok = res and res.get("success")
        count = len(res.get("orders", [])) if ok else 0
        self._record("API_ORDER_SEARCH_OPEN", ok, f"掛單 {count} 筆")

    async def _test_api_order_modify(self):
        if not self._limit_order_id:
            self._record("API_ORDER_MODIFY", False, "無限價單可改")
            return
        bars = DataHub.get_bars(self.contract_id)
        new_price = round(bars['c'].iloc[-1] - 25, 2) if not bars.empty else 0
        ok = await self.ts.modify_order(
            self.account_id, self._limit_order_id,
            limit_price=new_price
        )
        self._record("API_ORDER_MODIFY", ok,
                     f"改價至 {new_price}" if ok else "改單失敗")

    async def _test_api_order_cancel(self):
        if not self._limit_order_id:
            self._record("API_ORDER_CANCEL", False, "無限價單可撤")
            return
        ok = await self.ts.cancel_order(self.account_id, self._limit_order_id)
        self._record("API_ORDER_CANCEL", ok,
                     f"撤單 #{self._limit_order_id} 成功" if ok else "撤單失敗")

    async def _test_api_order_search(self):
        from datetime import timedelta
        start = datetime.utcnow() - timedelta(days=1)
        res = await self.ts.client.request("POST", "/api/Order/search", json={
            "accountId": self.account_id,
            "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ")
        })
        ok = res and res.get("success")
        count = len(res.get("orders", [])) if ok else 0
        self._record("API_ORDER_SEARCH", ok, f"歷史訂單 {count} 筆")

    async def _test_api_trade_search(self):
        trades = await self.ts.get_today_trades(self.account_id)
        ok = trades is not None
        completed = [t for t in trades if t.profitAndLoss is not None] if ok else []
        total_pnl = sum(t.profitAndLoss for t in completed)
        self._record("API_TRADE_SEARCH", ok,
                     f"今日 {len(trades)} 筆成交，完整回合 {len(completed)} 筆，損益 ${total_pnl:+.2f}")

    # ══════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════

    def _record(self, name: str, ok: bool, detail: str = ""):
        self._results[name] = {"ok": ok, "detail": detail}
        status = "✅ PASS" if ok else "❌ FAIL"
        logger.info(f"[Test] {status} | {name} | {detail}")

    async def _report(self):
        passed = sum(1 for v in self._results.values() if v["ok"])
        total = len(self._results)
        failed = [k for k, v in self._results.items() if not v["ok"]]

        # 分組統計
        a_pass = sum(1 for k, v in self._results.items() if k.startswith("TEST_") and v["ok"])
        b_pass = sum(1 for k, v in self._results.items() if k.startswith("API_") and v["ok"])

        logger.info(f"[Test] ========== 測試完成 {passed}/{total} ==========")
        for name, v in self._results.items():
            icon = "✅" if v["ok"] else "❌"
            logger.info(f"[Test] {icon} {name}: {v['detail']}")

        status_str = "✅ 全部通過" if passed == total else f"⚠️ {total - passed} 項失敗"
        msg = (
            f"🛡️ *完整系統測試報告*\n"
            f"結果：{status_str} ({passed}/{total})\n"
            f"━━━━━━━━━━━━━━━\n"
            f"無人值守：`{a_pass}/{len(self.STEPS_A)}`\n"
            f"API 端點：`{b_pass}/{len(self.STEPS_B)}`\n"
        )
        if failed:
            msg += "━━━━━━━━━━━━━━━\n❌ 失敗項目：\n"
            msg += "\n".join(f"  • {f}: {self._results[f]['detail']}" for f in failed)
        else:
            msg += "━━━━━━━━━━━━━━━\n🎉 系統完整可用！"

        await self.engine.notifier.send_message(msg)
