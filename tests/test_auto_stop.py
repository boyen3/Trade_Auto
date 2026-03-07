import asyncio
import sys
import time
from core.client import TopstepXClient
from services.account import AccountService
from services.order_monitor import OrderMonitor

# --- 交易設定 ---
STOP_LOSS_POINTS = 50.0
TICK_SIZE = 0.25

def round_to_tick(price):
    return round(price / TICK_SIZE) * TICK_SIZE

async def sync_stop_loss(client, monitor, account_id, pos_data):
    # 解析持倉
    contract = pos_data.get('contractId') if pos_data else None
    size = int(pos_data.get('size', 0)) if pos_data else 0
    
    # 從快照獲取現有止損
    existing_stops = monitor.get_active_stops(contract) if contract else []
    current_stop_size = sum(int(o['size']) for o in existing_stops)

    # 1. 數量對齊檢查 (防止無限循環)
    if size == current_stop_size:
        return

    print(f"⚖️ 同步啟動: 持倉 {size} | 現有止損 {current_stop_size}")

    # 2. 清理舊止損單
    if existing_stops:
        for o in existing_stops:
            await client.request("POST", "/api/Order/cancel", json={
                "accountId": int(account_id), "orderId": int(o['id'])
            })
        await asyncio.sleep(0.5)

    # 3. 補上新止損單
    if size > 0:
        avg_price = float(pos_data['averagePrice'])
        pos_type = pos_data['type']
        side = 1 if pos_type == 1 else 0 # 多單平倉用賣(1)，空單平倉用買(0)
        stop_price = round_to_tick(avg_price - STOP_LOSS_POINTS if pos_type == 1 else avg_price + STOP_LOSS_POINTS)

        # 核心修正：使用唯一標籤防止 API 拒絕
        unique_tag = f"as_{int(time.time() * 1000)}"

        payload = {
            "accountId": int(account_id),
            "contractId": contract,
            "type": 4, 
            "side": side,
            "size": size,
            "stopPrice": float(stop_price),
            "timeInForce": 1,
            "customTag": unique_tag
        }

        res = await client.request("POST", "/api/Order/place", json=payload)
        if res.get("success"):
            print(f"✅ 止損同步成功: {size}口 @ {stop_price} (Tag: {unique_tag})")
            # 關鍵：暫停 2 秒，等伺服器數據刷新，避免 Monitor 看到舊資料
            await asyncio.sleep(2.0)
        else:
            print(f"❌ 失敗: {res.get('errorMessage')}")

async def main():
    print("========================================")
    print("   TopstepX 自動防護系統 (正式交付版)   ")
    print("========================================")
    
    client = TopstepXClient()
    if not await client.login(): return

    acc_service = AccountService(client)
    account = await acc_service.get_primary_account()
    
    monitor = OrderMonitor(client)
    # 綁定同步回調
    monitor.on_position_update = lambda p: sync_stop_loss(client, monitor, account.id, p)

    try:
        await monitor.start(account_id=account.id, interval=3.0)
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()
        print("=== 系統關閉 ===")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())