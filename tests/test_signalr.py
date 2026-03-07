import asyncio
from core.client import TopstepXClient
from services.account import AccountService
from services.order_monitor import OrderMonitor

# 定義標準的回調函數 (零件化設計：外部定義邏輯，內部觸發)
def handle_order_event(order_id, status):
    print(f"\n[事件回報] 訂單 {order_id} 現在狀態為: {status}")

def handle_position_event(pos_data):
    size = pos_data.get('size', 0) if pos_data else 0
    contract = pos_data.get('contractId', 'N/A')
    print(f"\n[事件回報] 部位更新 | 合約: {contract} | 數量: {size}")

async def main():
    client = TopstepXClient()
    if not await client.login():
        return

    # 獲取帳戶 ID
    acc_service = AccountService(client)
    account = await acc_service.get_primary_account()
    print(f"🤖 自動選取首位帳戶: {account.name} ({account.id})")

    # 初始化監控零件
    monitor = OrderMonitor(client)
    
    # 綁定事件
    monitor.on_order_update = handle_order_event
    monitor.on_position_update = handle_position_event

    try:
        # 啟動監控 (Win7 建議 3.0 秒)
        await monitor.start(account_id=account.id, interval=3.0)
    except KeyboardInterrupt:
        print("\n使用者停止監控。")
        monitor.stop()
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())