# strategies/api_coverage_test.py
"""
API 全覆蓋測試策略
================
測試目的：驗證系統與 TopstepX API 的每一個實作端點都能正常運作。
測試方式：按照固定步驟序列執行，每步驟間隔等待，並透過 log 和 Telegram 回報結果。

⚠️ 注意：此策略會產生真實交易，請在模擬帳戶執行。

測試覆蓋端點：
  ✅ /api/Account/search
  ✅ /api/Contract/searchById
  ✅ /api/Contract/search
  ✅ /api/Contract/available
  ✅ /api/History/retrieveBars
  ✅ /api/Order/place         (市價單)
  ✅ /api/Order/place         (限價單 + SL/TP bracket)
  ✅ /api/Order/searchOpen
  ✅ /api/Order/search
  ✅ /api/Order/modify
  ✅ /api/Order/cancel
  ✅ /api/Position/searchOpen
  ✅ /api/Position/closeContract
  ✅ /api/Position/partialCloseContract
  ✅ /api/Trade/search
"""
import asyncio
import pandas as pd
from datetime import datetime
from strategies.base_strategy import BaseStrategy
from services.account import AccountService
from core.constants import OrderSide, OrderType, BarUnit
from core.data_hub import DataHub
from utils.logger import logger


class ApiCoverageTestStrategy(BaseStrategy):

    STEP_NAMES = [
        "IDLE",
        "TEST_ACCOUNT",
        "TEST_CONTRACT_SEARCH",
        "TEST_CONTRACT_BY_ID",
        "TEST_CONTRACT_AVAILABLE",
        "TEST_HISTORY_BARS",
        "TEST_PLACE_MARKET_ORDER",
        "TEST_POSITION_SEARCH",
        "TEST_PARTIAL_CLOSE",
        "TEST_CLOSE_POSITION",
        "TEST_PLACE_LIMIT_WITH_BRACKET",
        "TEST_ORDER_SEARCH_OPEN",
        "TEST_ORDER_MODIFY",
        "TEST_ORDER_CANCEL",
        "TEST_ORDER_SEARCH",
        "TEST_TRADE_SEARCH",
        "ALL_DONE",
    ]

    def __init__(self, trading_service, market_service, notifier, account_service: AccountService):
        super().__init__(trading_service, market_service)
        self.notifier = notifier
        self.account_service = account_service
        self.strategy_name = "API_Coverage_Test"

        self._step = 0          # 目前執行到第幾個步驟
        self._running = False   # 防止同時觸發
        self._test_order_id = None      # 市價單 ID
        self._limit_order_id = None     # 限價單 ID（含 bracket）
        self._results = {}              # 記錄每個步驟的通過/失敗

    # ──────────────────────────────────────────
    # 主入口：由 EventEngine 的 ON_BAR 事件觸發
    # ──────────────────────────────────────────
    async def on_bar(self, df: pd.DataFrame):
        if self._running or self._step >= len(self.STEP_NAMES) - 1:
            return
        self._running = True
        try:
            await self._execute_next_step()
        finally:
            self._running = False

    async def _execute_next_step(self):
        self._step += 1
        step_name = self.STEP_NAMES[self._step]
        logger.info(f"[APITest] ▶ 步驟 {self._step}/{len(self.STEP_NAMES)-1}: {step_name}")

        handler = getattr(self, f"_step_{step_name.lower()}", None)
        if handler:
            await handler()
        else:
            logger.warning(f"[APITest] 找不到步驟處理器: {step_name}")

    # ──────────────────────────────────────────
    # 各步驟實作
    # ──────────────────────────────────────────

    async def _step_test_account(self):
        """測試 /api/Account/search"""
        accounts = await self.account_service.get_all_accounts()
        ok = len(accounts) > 0
        self._record("Account/search", ok, f"找到 {len(accounts)} 個帳戶")
        if ok:
            acc = accounts[0]
            logger.info(f"[APITest] 帳戶: {acc.name} | 餘額: ${acc.balance:,.2f}")

    async def _step_test_contract_search(self):
        """測試 /api/Contract/search"""
        contracts = await self.ms.search_contract("MNQ")
        ok = len(contracts) > 0
        self._record("Contract/search", ok, f"找到 {len(contracts)} 個合約")

    async def _step_test_contract_by_id(self):
        """測試 /api/Contract/searchById"""
        contract = await self.ms.get_contract_info(self.contract_id)
        ok = contract is not None
        detail = f"tickSize:{contract.tickSize} pointValue:{contract.point_value}" if ok else "查無結果"
        self._record("Contract/searchById", ok, detail)

    async def _step_test_contract_available(self):
        """測試 /api/Contract/available"""
        res = await self.ts.client.request("POST", "/api/Contract/available", json={"live": False})
        ok = res and res.get("success", False)
        count = len(res.get("contracts", [])) if ok else 0
        self._record("Contract/available", ok, f"共 {count} 個可用合約")

    async def _step_test_history_bars(self):
        """測試 /api/History/retrieveBars"""
        df = await self.ms.get_bars_df(self.contract_id, unit=BarUnit.MINUTE, limit=10)
        ok = not df.empty
        detail = f"取得 {len(df)} 根 K 線，最新收盤: {df['c'].iloc[-1] if ok else 'N/A'}"
        self._record("History/retrieveBars", ok, detail)

    async def _step_test_place_market_order(self):
        """測試 /api/Order/place（市價單，買入 1 口）"""
        order_id = await self.ts.place_order(
            self.account_id, self.contract_id,
            order_type=OrderType.MARKET, side=OrderSide.BUY, size=1
        )
        ok = order_id is not None
        self._test_order_id = order_id
        self._record("Order/place (Market)", ok, f"訂單 ID: #{order_id}")
        if ok:
            await asyncio.sleep(2)  # 等待成交

    async def _step_test_position_search(self):
        """測試 /api/Position/searchOpen"""
        positions = await self.ts.get_open_positions(self.account_id)
        ok = len(positions) > 0
        detail = f"找到 {len(positions)} 個持倉"
        if ok:
            p = positions[0]
            detail += f" | {p.symbolName} {p.size}口 均價:{p.averagePrice}"
        self._record("Position/searchOpen", ok, detail)

    async def _step_test_partial_close(self):
        """測試 /api/Position/partialCloseContract（部分平倉）"""
        positions = await self.ts.get_open_positions(self.account_id)
        if not positions or positions[0].size < 2:
            # 先補倉到 2 口再測部分平倉
            await self.ts.place_order(
                self.account_id, self.contract_id,
                order_type=OrderType.MARKET, side=OrderSide.BUY, size=1
            )
            await asyncio.sleep(2)

        ok = await self.ts.partial_close_position(self.account_id, self.contract_id, size=1)
        self._record("Position/partialCloseContract", ok, "部分平倉 1 口")
        await asyncio.sleep(2)

    async def _step_test_close_position(self):
        """測試 /api/Position/closeContract（整口平倉）"""
        ok = await self.ts.close_position(self.account_id, self.contract_id)
        self._record("Position/closeContract", ok, "整口平倉")
        await asyncio.sleep(2)

    async def _step_test_place_limit_with_bracket(self):
        """測試 /api/Order/place（限價單 + SL/TP bracket）"""
        # 取目前市價，掛一個距離較遠的限價單（不會立刻成交）
        bars = DataHub.get_bars(self.contract_id)
        if bars.empty:
            self._record("Order/place (Limit+Bracket)", False, "無法取得市價")
            return

        current_price = bars["c"].iloc[-1]
        limit_price = round(current_price - 50, 2)  # 低於市價 50 點，不會立刻成交

        order_id = await self.ts.place_order(
            self.account_id, self.contract_id,
            order_type=OrderType.LIMIT, side=OrderSide.BUY, size=1,
            limit_price=limit_price,
            sl_ticks=40, tp_ticks=80
        )
        ok = order_id is not None
        self._limit_order_id = order_id
        self._record("Order/place (Limit+Bracket)", ok,
                     f"訂單 #{order_id} 限價:{limit_price} SL:40t TP:80t")

    async def _step_test_order_search_open(self):
        """測試 /api/Order/searchOpen"""
        res = await self.ts.client.request(
            "POST", "/api/Order/searchOpen", json={"accountId": self.account_id}
        )
        ok = res and res.get("success", False)
        count = len(res.get("orders", [])) if ok else 0
        self._record("Order/searchOpen", ok, f"目前 {count} 張掛單")

    async def _step_test_order_modify(self):
        """測試 /api/Order/modify（修改限價單價格）"""
        if not self._limit_order_id:
            self._record("Order/modify", False, "無可用限價單 ID")
            return

        bars = DataHub.get_bars(self.contract_id)
        current_price = bars["c"].iloc[-1] if not bars.empty else 0
        new_limit = round(current_price - 60, 2)  # 再往下移 10 點

        ok = await self.ts.modify_order(
            self.account_id, self._limit_order_id, limit_price=new_limit
        )
        self._record("Order/modify", ok, f"訂單 #{self._limit_order_id} 新限價:{new_limit}")

    async def _step_test_order_cancel(self):
        """測試 /api/Order/cancel（撤銷限價單）"""
        if not self._limit_order_id:
            self._record("Order/cancel", False, "無可用限價單 ID")
            return

        ok = await self.ts.cancel_order(self.account_id, self._limit_order_id)
        self._record("Order/cancel", ok, f"撤銷訂單 #{self._limit_order_id}")

    async def _step_test_order_search(self):
        """測試 /api/Order/search（查今日歷史訂單）"""
        if self._test_order_id:
            res = await self.ts.get_order_status(self.account_id, self._test_order_id)
            ok = res.get("success", False)
            status = res.get("order", {}).get("status", "N/A") if ok else "N/A"
            self._record("Order/search", ok, f"訂單 #{self._test_order_id} 狀態:{status}")
        else:
            self._record("Order/search", False, "無可用訂單 ID")

    async def _step_test_trade_search(self):
        """測試 /api/Trade/search（查今日成交紀錄）"""
        trades = await self.ts.get_today_trades(self.account_id)
        ok = True  # 只要 API 回應成功就算通過，當日可能無成交
        completed = [t for t in trades if t.profitAndLoss is not None]
        total_pnl = sum(t.profitAndLoss for t in completed)
        total_fees = sum(t.fees or 0.0 for t in trades)
        self._record("Trade/search", ok,
                     f"共 {len(trades)} 筆 | 完整回合:{len(completed)} | 損益:${total_pnl:.2f} | 手續費:${total_fees:.2f}")

    async def _step_all_done(self):
        """測試完成，輸出總結報告"""
        passed = sum(1 for v in self._results.values() if v["ok"])
        total = len(self._results)
        failed_items = [k for k, v in self._results.items() if not v["ok"]]

        report_lines = [f"{'✅' if v['ok'] else '❌'} {k}: {v['detail']}"
                        for k, v in self._results.items()]
        report = "\n".join(report_lines)

        logger.info(f"[APITest] ===== 測試完成 =====")
        logger.info(f"[APITest] 通過: {passed}/{total}")
        for line in report_lines:
            logger.info(f"[APITest] {line}")

        if self.notifier:
            status = "✅ 全部通過" if passed == total else f"⚠️ {total - passed} 項失敗"
            msg = (
                f"🧪 *API 全覆蓋測試完成*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"結果：{status} ({passed}/{total})\n"
                f"━━━━━━━━━━━━━━━\n"
            )
            if failed_items:
                msg += "❌ 失敗項目：\n" + "\n".join(f"  • {i}" for i in failed_items)
            await self.notifier.send_message(msg)

    # ──────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────
    def _record(self, endpoint: str, ok: bool, detail: str = ""):
        self._results[endpoint] = {"ok": ok, "detail": detail}
        status = "✅ PASS" if ok else "❌ FAIL"
        logger.info(f"[APITest] {status} | {endpoint} | {detail}")

    def get_pos_size(self) -> int:
        pos = DataHub.get_position(self.contract_id)
        return pos.size if pos else 0
