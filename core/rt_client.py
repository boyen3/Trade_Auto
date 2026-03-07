import threading
import time
from signalrcore.hub_connection_builder import HubConnectionBuilder
from typing import Dict, Callable, Optional

class RealTimeClient:
    def __init__(self, token: str, account_id: int):
        self.user_hub_url = f"https://rtc.topstepx.com/hubs/user?access_token={token}"
        self.market_hub_url = f"https://rtc.topstepx.com/hubs/market?access_token={token}"
        self.account_id = account_id
        
        # 緩存：讓策略隨時讀取最新狀態，不需要等回調
        self.latest_quotes: Dict[str, Dict] = {}
        self.active_orders: Dict[int, Dict] = {}
        self.positions: Dict[str, Dict] = {}

        self.user_conn = None
        self.market_conn = None

    def start(self):
        """啟動 Real-Time 連線 (零件初始化)"""
        # 1. 設置 User Hub (訂單、部位)
        self.user_conn = HubConnectionBuilder()\
            .with_url(self.user_hub_url, options={"access_token_factory": lambda: self.token})\
            .with_automatic_reconnect({
                "type": "interval",
                "intervals": [1, 5, 10, 30]
            }).build()

        # 2. 綁定事件處理器 (將推播內容存入零件緩存)
        self.user_conn.on("GatewayUserOrder", self._on_order_update)
        self.user_conn.on("GatewayUserPosition", self._on_position_update)
        
        # 3. 啟動連線並訂閱
        self.user_conn.start()
        time.sleep(2) # 等待握手
        self.user_conn.send("SubscribeOrders", [self.account_id])
        self.user_conn.send("SubscribePositions", [self.account_id])

        print("✅ Real-Time 基礎建設：User Hub 已上線")

    def _on_order_update(self, data):
        """零件內部邏輯：當訂單狀態改變時自動更新快照"""
        order_data = data[0]
        order_id = order_data['id']
        self.active_orders[order_id] = order_data
        print(f"🔔 訂單狀態更新: ID {order_id}, Status: {order_data['status']}")

    def _on_position_update(self, data):
        pos_data = data[0]
        self.positions[pos_data['contractId']] = pos_data

    def get_order_status(self, order_id: int) -> Optional[int]:
        """通用零件介面：給予 ID 立即回傳狀態 (0 延遲)"""
        order = self.active_orders.get(order_id)
        return order['status'] if order else None