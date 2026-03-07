import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.client import TopstepXClient
from services.account import AccountService
from services.market_data import MarketDataService
from services.trading import TradingService

async def run_sync_test():
    print("🚀 [ProjectX] 啟動全域組件同步測試 (含防錯機制)...")
    client = TopstepXClient()
    acc_service = AccountService(client)
    mkt_service = MarketDataService(client)
    trade_service = TradingService(client)
    
    try:
        account = await acc_service.get_primary_account()
        if not account:
            print("❌ 錯誤：無法獲取帳戶")
            return

        # 搜尋 MNQ，若無則搜尋 NQ
        contracts = await mkt_service.search_contract("MNQ")
        if not contracts:
            print("🔍 MNQ 搜尋不到，嘗試搜尋 NQ...")
            contracts = await mkt_service.search_contract("NQ")
        
        if not contracts:
            print("❌ 致命錯誤：搜尋不到任何 NQ/MNQ 合約。請確認 API 狀態或市場是否開市。")
            return

        target = contracts[0]
        print(f"✅ 成功鎖定合約：{target.name} (ID: {target.id})")

        df = await mkt_service.get_bars_df(target.id, limit=3)
        if df.empty:
            print("⚠️ 警告：獲取的 K 線數據為空，可能目前無交易。")
        else:
            print(f"📊 最近數據：\n{df}")

        print(f"🚀 發送測試買單 (1口 {target.name})...")
        res = await trade_service.place_order(account.id, target.id, 2, 0, size=1, sl_ticks=40)
        
        if res.success:
            print(f"✅ 訂單成功：ID {res.orderId}")
        else:
            print(f"❌ 訂單失敗：{res.errorMessage}")

    except Exception as e:
        # 詳細印出錯誤類型與內容，方便除錯
        import traceback
        print(f"❌ 嚴重系統異常：{str(e)}")
        traceback.print_exc()
    finally:
        await client.close()
        print("🏁 測試結束")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_sync_test())