# services/trading.py
import asyncio
from core.constants import OrderSide, OrderType
from models.order import PositionModel
from typing import List, Dict, Optional
from utils.logger import logger

class TradingService:
    def __init__(self, client, notifier=None):
        self.client = client
        self.notifier = notifier
        self.pending_orders: Dict[int, dict] = {}  # {order_id: {contract_id, side, size}}

    async def place_order(self, account_id: int, contract_id: str, order_type: int, side: int, size: int, 
                          limit_price: float = None, stop_price: float = None, 
                          sl_ticks: int = None, tp_ticks: int = None, 
                          **kwargs) -> Optional[int]:
        """
        支援自動方向修正的通用下單接口
        :param sl_ticks: 止損跳動點數 (正整數)，系統會根據 side 自動轉正負
        :param tp_ticks: 止盈跳動點數 (正整數)，系統會根據 side 自動轉正負
        """
        # 1. 基礎 Payload
        payload = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": int(order_type),
            "side": int(side),
            "size": int(size),
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "trailPrice": kwargs.get("trailPrice"),
            "customTag": kwargs.get("customTag")
        }
        
        # 2. 自動正負號轉換邏輯 (根據 TopstepX 規範)
        # 做多(Side 0): SL 必須為負, TP 必須為正
        # 做空(Side 1): SL 必須為正, TP 必須為負
        if sl_ticks is not None:
            actual_sl = -abs(sl_ticks) if side == 0 else abs(sl_ticks)
            payload["stopLossBracket"] = {
                "ticks": actual_sl,
                "type": kwargs.get("sl_type", 4) # 4 = Stop
            }
            
        if tp_ticks is not None:
            actual_tp = abs(tp_ticks) if side == 0 else -abs(tp_ticks)
            payload["takeProfitBracket"] = {
                "ticks": actual_tp,
                "type": kwargs.get("tp_type", 1) # 1 = Limit
            }

        # 3. 移除 None 值以保持 Payload 潔淨
        clean_payload = {k: v for k, v in payload.items() if v is not None}

        logger.info(f"📤 [下單請求] {contract_id} | 側向:{side} | SL:{sl_ticks} -> {payload.get('stopLossBracket', {}).get('ticks')} | TP:{tp_ticks} -> {payload.get('takeProfitBracket', {}).get('ticks')}")
        
        res_data = await self.client.request("POST", "/api/Order/place", json=clean_payload)
        
        if res_data and res_data.get("success"):
            order_id = res_data.get("orderId")
            self.pending_orders[order_id] = {
                "contract_id": contract_id,
                "side": side,
                "size": size
            }
            logger.info(f"✅ [訂單成功] 編號: {order_id}")
            return order_id
        
        err_msg = res_data.get("errorMessage", "未知錯誤")
        logger.error(f"❌ [下單失敗] {err_msg}")
        return None

    async def get_order_status(self, account_id: int, order_id: int) -> dict:
        """
        查詢單一訂單狀態。
        API 無獨立 status 端點，改用 searchOpen 先查掛單，
        找不到再用 search 查歷史（代表已成交或已取消）。
        回傳格式統一為 {"success": bool, "order": {...}}
        """
        from datetime import datetime, timedelta

        # 1. 先查掛單（最快）
        res = await self.client.request(
            "POST", "/api/Order/searchOpen",
            json={"accountId": account_id}
        )
        if res and res.get("success"):
            for o in res.get("orders", []):
                if o.get("id") == order_id:
                    return {"success": True, "order": {**o, "status": 1}}  # 1 = 仍掛單

        # 2. 查不到掛單 → 查今日歷史訂單（成交/取消）
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        res2 = await self.client.request(
            "POST", "/api/Order/search",
            json={"accountId": account_id, "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ")}
        )
        if res2 and res2.get("success"):
            for o in res2.get("orders", []):
                if o.get("id") == order_id:
                    # status 2=Filled, 3=Cancelled, 4=Rejected
                    return {"success": True, "order": o}

        return {"success": False, "order": {}}

    async def cancel_order(self, account_id: int, order_id: int):
        # 根據文件，撤單通常使用相同的路徑或特定路徑，保持原狀即可
        payload = {"accountId": account_id, "orderId": order_id}
        return await self.client.request("POST", "/api/Order/cancel", json=payload)

    async def modify_order(self, account_id: int, order_id: int,
                           limit_price: float = None, stop_price: float = None,
                           size: int = None) -> bool:
        """
        改單：修改限價單的價格或口數。
        :param limit_price: 新的限價（限價單用）
        :param stop_price:  新的停損觸發價（停損單用）
        :param size:        新的口數
        """
        payload = {"accountId": account_id, "orderId": order_id}
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if size is not None:
            payload["size"] = size

        logger.info(f"📝 [改單請求] 訂單 #{order_id} | limitPrice:{limit_price} stopPrice:{stop_price} size:{size}")
        res = await self.client.request("POST", "/api/Order/modify", json=payload)
        ok = res and res.get("success", False)
        if ok:
            logger.info(f"✅ [改單成功] 訂單 #{order_id}")
        else:
            logger.error(f"❌ [改單失敗] 訂單 #{order_id} | {res.get('errorMessage', '未知錯誤') if res else '無回應'}")
        return ok

    async def close_position(self, account_id: int, contract_id: str) -> bool:
        """整口平倉"""
        payload = {"accountId": account_id, "contractId": contract_id}
        logger.info(f"📤 [平倉請求] 帳戶:{account_id} 合約:{contract_id}")
        res = await self.client.request("POST", "/api/Position/closeContract", json=payload)
        ok = res and res.get("success", False)
        if ok:
            logger.info(f"✅ [平倉成功] {contract_id}")
        else:
            logger.error(f"❌ [平倉失敗] {contract_id} | {res.get('errorMessage', '未知') if res else '無回應'}")
        return ok

    async def partial_close_position(self, account_id: int, contract_id: str, size: int) -> bool:
        """部分平倉"""
        payload = {"accountId": account_id, "contractId": contract_id, "size": size}
        logger.info(f"📤 [部分平倉請求] 帳戶:{account_id} 合約:{contract_id} 口數:{size}")
        res = await self.client.request("POST", "/api/Position/partialCloseContract", json=payload)
        ok = res and res.get("success", False)
        if ok:
            logger.info(f"✅ [部分平倉成功] {contract_id} {size}口")
        else:
            logger.error(f"❌ [部分平倉失敗] {contract_id} | {res.get('errorMessage', '未知') if res else '無回應'}")
        return ok

    async def get_today_trades(self, account_id: int) -> list:
        """查詢今日所有成交記錄"""
        from datetime import datetime, timedelta
        from models.order import TradeModel
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {
            "accountId": account_id,
            "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        res = await self.client.request("POST", "/api/Trade/search", json=payload)
        trades = []
        if res and res.get("success"):
            raw_list = res.get("trades", [])
            logger.info(f"[TRADE_QUERY] 今日共 {len(raw_list)} 筆成交")
            for t in raw_list:
                try:
                    trades.append(TradeModel(
                        id=t.get("id", 0),
                        accountId=account_id,
                        orderId=t.get("orderId", 0),
                        contractId=t.get("contractId", ""),
                        side=t.get("side", 0),
                        size=t.get("size", 0),
                        price=t.get("price", 0.0),
                        profitAndLoss=t.get("profitAndLoss"),
                        fees=t.get("fees"),
                        creationTimestamp=t.get("creationTimestamp", t.get("createdAt", ""))
                    ))
                except Exception as e:
                    logger.warning(f"⚠️ 解析成交記錄失敗: {e}")
        return trades

    async def get_open_positions(self, account_id: int) -> List[PositionModel]:
        """
        獲取目前掛載的持倉 (供 OrderMonitor 同步至 DataHub)
        """
        data = await self.client.request("POST", "/api/Position/searchOpen", json={"accountId": account_id})
        positions = []
        # FIX #7: data 可能是 None（API 失敗），加 null guard
        if not data or not data.get("success"):
            return positions
        raw_list = data.get("positions", [])
        
        for p in raw_list:
            try:
                positions.append(PositionModel(
                    id=p.get('id', 0),
                    contractId=p.get('contractId', ''),
                    symbolName=p.get('contractId', 'Unknown').split('.')[-1],
                    side=p.get('side', 0), 
                    size=p.get('size', 0),
                    averagePrice=p.get('averagePrice', 0.0),
                    currentPrice=p.get('currentPrice', 0.0)
                ))
            except Exception as e:
                logger.warning(f"⚠️ 解析持倉數據失敗: {e}")
                
        return positions