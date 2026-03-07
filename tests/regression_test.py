import asyncio
import sys
from datetime import datetime
from core.client import TopstepXClient
from core.constants import OrderSide, OrderType
from services.account import AccountService
from services.market_data import MarketDataService
from services.trading import TradingService
from services.order_monitor import OrderMonitor

# Windows 7 / Python 3.8 兼容性配置
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def run_integration_test():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 開始 ProjectX 全模組集成測試...")
    print("=" * 60)
    
    client = TopstepXClient()
    test_order_id = None
    
    try:
        # 1. 認證與服務初始化
        if not await client.login():
            print("❌ 認證失敗")
            return

        acc_service = AccountService(client)
        market_service = MarketDataService(client)
        trade_service = TradingService(client)
        monitor = OrderMonitor(trade_service, market_service)

        # 2. 帳戶與合約獲取
        primary_acc = await acc_service.get_primary_account()
        contracts = await market_service.search_contract("MNQ")
        target_contract = contracts[0].id
        print(f"✅ 帳戶選定: {primary_acc.name}")
        print(f"✅ 合約選定: {target_contract} (每點價值: ${contracts[0].point_value})")

        # 3. 行情驗證
        df = await market_service.get_bars_df(target_contract, limit=5)
        if not df.empty:
            print(f"✅ 行情獲取正常，目前市價: {df['c'].iloc[-1]}")

        # 4. 模擬交易啟動 (下一個遠離市價的限價單，以便監控器觀察)
        limit_price = df['c'].iloc[-1] - 50  # 下在市價下方 50 點
        print(f"⏳ 正在發送測試單 (限價: {limit_price}, SL: 10 ticks, TP: 20 ticks)...")
        
        order_res = await trade_service.place_order(
            account_id=primary_acc.id,
            contract_id=target_contract,
            order_type=OrderType.LIMIT,
            side=OrderSide.BUY,
            size=1,
            sl_ticks=10,
            tp_ticks=20
        )

        if order_res.success:
            test_order_id = order_res.orderId
            print(f"✅ 下單成功，訂單編號: {test_order_id}")
        else:
            print(f"❌ 下單失敗: {order_res.errorMessage}")
            return

        # 5. 啟動監控器進行短暫輪詢 (測試 10 秒)
        print("\n--- 進入監控模式 (測試動態損益計算) ---")
        monitor_task = asyncio.create_task(monitor.start(primary_acc.id, interval=2.0))
        
        # 讓它跑 10 秒看輸出
        await asyncio.sleep(10)
        monitor.stop()
        await monitor_task
        print("\n--- 監控測試結束 ---")

        # 6. 成交紀錄驗證
        trades = await trade_service.get_recent_trades(primary_acc.id, limit=3)
        print(f"✅ 歷史成交紀錄讀取正常 (共 {len(trades)} 筆)")

    except Exception as e:
        print(f"💥 測試崩潰: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 7. 安全清理
        if test_order_id:
            print(f"\n⏳ [Cleanup] 正在撤銷測試單 {test_order_id}...")
            await trade_service.cancel_order(primary_acc.id, test_order_id)
            print("✅ [Cleanup] 測試現場已清理。")
        print("=" * 60)
        print("🎉 集成測試完成！")

if __name__ == "__main__":
    try:
        asyncio.run(run_integration_test())
    except KeyboardInterrupt:
        pass