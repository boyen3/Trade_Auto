# services/order_monitor.py
import asyncio
from datetime import datetime
from typing import List
from services.trading import TradingService
from services.market_data import MarketDataService
from core.data_hub import DataHub
from utils.logger import logger

class OrderMonitor:
    def __init__(self, trading_service: TradingService, market_service: MarketDataService, notifier=None):
        self.ts = trading_service
        self.ms = market_service
        self.notifier = notifier
        self._is_running = False
        self.current_balance = 0.0
        self._last_logged_balance = 0.0
        self._last_logged_pnl = 0.0

    async def start(self, account_id: int, interval: float = 2.0):
        """啟動監控引擎：負責同步帳戶盈虧與持倉狀態"""
        self._is_running = True
        logger.info(f"🛡️ 監控系統啟動 | 模式: 帳戶與訂單全同步 | 間隔: {interval}s")

        while self._is_running:
            try:
                # --- 1. 訂單成交回饋掃描 ---
                await self._track_pending_orders(account_id)

                # --- 2. 帳戶數據更新 (將最新餘額與已實現盈虧寫入 DataHub) ---
                acc_data = await self.ts.client.request("POST", "/api/Account/search", json={"onlyActiveAccounts": True})
                if acc_data and acc_data.get("accounts"):
                    for acc in acc_data["accounts"]:
                        if acc["id"] == account_id:
                            # 局部引入模型以避免循環依賴
                            from models.account import AccountModel
                            
                            # 建立結構化模型，處理 API 鍵值與預設值
                            # 從 Trade/search API 取今日已實現損益
                            realized = await self._get_daily_realized_pnl(account_id)
                            account_model = AccountModel(
                                id=acc.get("id"),
                                name=acc.get("name"),
                                balance=acc.get("balance", 0.0),
                                realizedPnL=realized,
                                canTrade=acc.get("canTrade", True),
                                isVisible=acc.get("isVisible", True)
                            )
                            # 更新中央數據庫
                            DataHub.update_account(account_model)
                            self.current_balance = account_model.balance

                # --- 3. 持倉數據與即時浮動損益同步 ---
                positions = await self.ts.get_open_positions(account_id)
                
                total_unrealized_pnl = 0.0
                if not positions:
                    DataHub.update_positions([])
                    print(f"\r[{datetime.now().strftime('%H:%M:%S')}] 🛡️ 監控中 | 帳戶餘額: ${self.current_balance:,.2f} | 無持倉...", end="", flush=True)
                else:
                    for pos in positions:
                        # 補齊合約的跳動點資訊 (PointValue)
                        contract = await self.ms.get_contract_info(pos.contractId)
                        if contract:
                            pos.pointValue = contract.point_value
                        
                        # 從 DataHub 獲取最新的行情價格
                        latest_bars = DataHub.get_bars(pos.contractId)
                        if not latest_bars.empty:
                            # 更新持倉對象的當前價格，觸發內部 PnL 計算邏輯
                            pos.currentPrice = latest_bars['c'].iloc[-1]
                        
                        total_unrealized_pnl += pos.unrealized_pnl_usd
                    
                    # 將更新後的持倉列表存回 DataHub
                    DataHub.update_positions(positions)
                    self._display_summary(positions, total_unrealized_pnl)

            except Exception as e:
                logger.error(f"❌ OrderMonitor 運作異常: {str(e)}")
            
            await asyncio.sleep(interval)

    async def _track_pending_orders(self, account_id: int):
        """檢查掛單狀態，成交後從追蹤清單移除"""
        pending_ids = list(self.ts.pending_orders.keys())
        for oid in pending_ids:
            try:
                status_res = await self.ts.get_order_status(account_id, oid)
                if status_res.get("success"):
                    order_info = status_res.get("order", {})
                    status = order_info.get("status")
                    if status == 2: # Filled
                        logger.info(f"🎊 [成交回饋] 訂單 {oid} 已完全成交！")
                        del self.ts.pending_orders[oid]
                    elif status in [3, 4]: # Cancelled/Rejected
                        logger.warning(f"🚫 [訂單失效] 訂單 {oid} 狀態: {status}")
                        del self.ts.pending_orders[oid]
            except Exception as e:
                logger.error(f"⚠️ 追蹤訂單 {oid} 失敗: {e}")

    def _display_summary(self, positions: List, total_pnl: float):
        """終端機美化面板"""
        print("\n" + "="*55)
        print(f"⏰ 同步時間: {datetime.now().strftime('%H:%M:%S')}")
        print(f"💰 帳戶餘額: ${self.current_balance:,.2f} | 當前浮盈: ${total_pnl:+.2f}")
        print("-" * 55)
        for pos in positions:
            side_str = "🟢 BUY" if pos.side == 0 else "🔴 SELL"
            print(f"📍 {pos.symbolName: <10} | {side_str} {pos.size: >2}口 | 均價: {pos.averagePrice: >9.2f} | 盈虧: ${pos.unrealized_pnl_usd: >8.2f}")
        print("="*55 + "\n")


    async def _get_daily_realized_pnl(self, account_id: int) -> float:
        try:
            from datetime import date
            today = date.today().strftime("%Y-%m-%dT00:00:00Z")
            payload = {"accountId": account_id, "startTimestamp": today}
            data = await self.ts.client.request("POST", "/api/Trade/search", json=payload)
            if not data or not data.get("success"):
                logger.info(f"[OrderMonitor] Trade/search 失敗或無資料: {data}")
                return 0.0
            trades = data.get("trades", [])
            # API 欄位是 profitAndLoss，null 表示開倉半回（進場），有值才是出場
            total = sum(float(t.get("profitAndLoss") or 0) for t in trades if t.get("profitAndLoss") is not None)
            n = len(trades)
            if n != getattr(self, '_last_trade_count', -1) or abs(total - getattr(self, '_last_trade_pnl', -999)) > 0.01:
                logger.info(f"[OrderMonitor] 今日成交 {n} 筆，已實現: ${total:+.2f}")
                self._last_trade_count = n
                self._last_trade_pnl   = total
            return total
        except Exception as e:
            logger.info(f"[OrderMonitor] 損益查詢異常: {e}")
            return 0.0

    def stop(self):
        self._is_running = False