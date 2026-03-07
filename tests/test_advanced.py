import asyncio
import sys
import os
from datetime import datetime

# 路徑處理
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.client import TopstepXClient
from services.account import AccountService
from services.market_data import MarketDataService
from services.trading import TradingService

async def run_advanced_test():
    client = TopstepXClient()
    acc_service = AccountService(client)
    mkt_service = MarketDataService(client)
    trade_service = TradingService(client)
    
    try:
        # 1. 初始化
        account = await acc_service.get_primary_account()
        # 換合約測試：同時搜尋 MNQ 和 MES (微型標普)
        mnq_contracts = await mkt_service.search_contract("MNQ")
        mes_contracts = await mkt_service.search_contract("MES")
        
        mnq = mnq_contracts[0]
        mes = mes_contracts[0]

        print(f"--- [場景 1: 加倉測試 (Scaling In)] ---")
        # 先買入 1 口 MNQ
        print(f"🚀 買入第 1 口 {mnq.name}...")
        res1 = await trade_service.place_order(account.id, mnq.id, 2, 0, size=1)
        await asyncio.sleep(2)
        # 再次買入 1 口 MNQ (加倉)
        print(f"🚀 加倉第 2 口 {mnq.name}...")
        res2 = await trade_service.place_order(account.id, mnq.id, 2, 0, size=1)
        
        pos = await trade_service.get_open_positions(account.id)
        for p in pos:
            if p.contractId == mnq.id:
                print(f"✅ 當前 {mnq.name} 總持倉: {p.size} 口 (均價: {p.averagePrice})")

        print(f"\n--- [場景 2: 移動止損 (Trailing Stop) 測試] ---")
        # 根據文件，type=5 是 TrailingStop。這裡我們手動發送一個追蹤止損
        # 注意：Trailing Stop 通常需要設定 stopPrice 或 trailPrice
        print(f"🚀 為 {mnq.name} 設定移動止損...")
        trailing_res = await client.request("POST", "/api/Order/place", json={
            "accountId": account.id,
            "contractId": mnq.id,
            "type": 5, # TrailingStop
            "side": 1, # 因為是多單，止損是賣出
            "size": 2,
            "stopPrice": mnq.tickSize * 10, # 這裡視 API 具體要求而定
            "trailPrice": 5.0 # 追蹤距離
        })
        print(f"✅ 移動止損回報: {trailing_res.get('success')}")

        print(f"\n--- [場景 3: 換合約操作 (Cross-Asset)] ---")
        print(f"🚀 同時操作另一合約: 買入 {mes.name}...")
        await trade_service.place_order(account.id, mes.id, 2, 0, size=1)
        
        pos_all = await trade_service.get_open_positions(account.id)
        print(f"✅ 目前所有持倉合約: {[p.contractId for p in pos_all]}")

        print(f"\n--- [場景 4: 損益回報與主動平倉] ---")
        # 查詢 Trades 回報損益 (這會抓取最近的交易紀錄)
        trades_data = await client.request("POST", "/api/Trade/search", json={
            "accountId": account.id,
            "startTimestamp": datetime(2025, 1, 1).isoformat() + "Z"
        })
        recent_trades = trades_data.get("trades", [])[-2:] # 拿最後兩筆
        for t in recent_trades:
            print(f"💰 交易紀錄 ID: {t.get('id')} | 價格: {t.get('price')} | 損益: {t.get('profitAndLoss')}")

        print(f"\n🧹 執行主動離場 (Flatten All)...")
        await trade_service.close_position(account.id, mnq.id)
        await trade_service.close_position(account.id, mes.id)
        print("✅ 所有測試倉位已清理")

    except Exception as e:
        print(f"❌ 測試中斷: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(run_advanced_test())