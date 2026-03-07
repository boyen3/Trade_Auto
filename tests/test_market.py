import asyncio
import sys
import os
import pandas as pd

# 修正路徑
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.client import TopstepXClient
from services.market_data import MarketDataService

async def run_test():
    print("--- [Market Data 測試] ---")
    client = TopstepXClient()
    market_service = MarketDataService(client)
    
    try:
        # 1. 搜尋 NQ 合約
        print("🔍 正在搜尋 NQ 合約...")
        contracts = await market_service.search_contract("NQ")
        if not contracts:
            print("❌ 找不到合約")
            return
        
        target = contracts[0]
        print(f"✅ 找到合約: {target.name} (ID: {target.id})")
        print(f"   Tick Size: {target.tickSize} | Tick Value: {target.tickValue}")

        # 2. 拉取 1 分鐘 K 線 (unit=2)
        print(f"📊 正在拉取 {target.name} 的 1 分鐘 K 線數據...")
        df = await market_service.get_bars_df(contract_id=target.id, unit=2, limit=100)
        
        if not df.empty:
            print("\n✅ 成功獲取 DataFrame:")
            print(df.tail(10)) # 印出最後 10 根
            
            # 測試計算一個簡單的 MA (量化基礎)
            df['MA5'] = df['close'].rolling(5).mean()
            print("\n📈 簡單指標測試 (MA5):")
            print(df[['close', 'MA5']].tail(5))
        else:
            print("⚠️ 未獲取到任何 K 線數據。")

    except Exception as e:
        print(f"❌ 測試發生錯誤: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_test())