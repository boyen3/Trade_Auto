import asyncio
import sys
from datetime import datetime
from core.client import TopstepXClient
from services.market_data import MarketDataService
from services.trading import TradingService
from services.order_monitor import OrderMonitor
from services.risk_manager import RiskManager

# 確保 Windows 7 上的事件迴圈相容性
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def run_final_test():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏁 開始全系統邏輯強化測試...")
    
    # 1. 初始化核心客戶端
    client = TopstepXClient()
    
    # 2. 初始化各項服務
    market_service = MarketDataService(client)
    trade_service = TradingService(client)
    # 注入 market_service 到 trading 以便計算損益
    trade_service.market_service = market_service 
    
    risk_manager = RiskManager(trade_service)
    monitor = OrderMonitor(trade_service, market_service)

    try:
        # 3. 執行預載 (解決損益驚悚數字的關鍵)
        await market_service.preload_all_contracts()
        
        # 4. 獲取帳戶資訊
        # 假設我們直接拿第一個可用帳戶
        data = await client.request("POST", "/api/Account/search", json={})
        if not data:
            print("❌ 無法獲取帳戶資訊，請檢查 API Key。")
            return
        
        # 強化帳戶解析邏輯
        accounts = []
        if isinstance(data, list):
            accounts = data
        elif isinstance(data, dict):
            accounts = data.get("accounts", []) or data.get("data", [])

        if not accounts:
            print(f"❌ 找不到可用帳戶，API 回傳內容: {data}")
            return
        
        # 安全獲取首個帳戶
        primary_acc = accounts[0]
        account_id = primary_acc.get('id') or primary_acc.get('accountId')
        account_name = primary_acc.get('name') or primary_acc.get('description', 'Unknown')
        
        if not account_id:
            print("❌ 帳戶數據中缺少 ID 欄位")
            return

        print(f"✅ 測試帳戶識別成功: {account_name} ({account_id})")
        print(f"✅ 測試帳戶: {account_name} ({account_id})")

        # 5. 進入「預演監控」模式
        print("\n--- 🕵️ 進入 15 秒邏輯驗證監控 (含風控檢查) ---")
        
        for i in range(5): # 進行 5 次輪詢檢查
            print(f"\n[檢查輪次 {i+1}/5]")
            
            # 執行風控安全檢查 (內部會自動修正點值)
            is_safe = await risk_manager.check_safety(account_id)
            
            if not is_safe:
                print("🚨 風控觸發！測試終止。")
                break
                
            # 模擬監控輸出
            positions = await trade_service.get_open_positions(account_id)
            if not positions:
                print("ℹ️ 目前帳戶無持倉，損益正常計算中...")
            else:
                for p in positions:
                    # 確認點值是否已被 market_service 校正
                    contract = await market_service.get_contract_info(p.contractId)
                    if contract:
                        p.pointValue = contract.point_value
                    print(f"📍 商品: {p.symbolName} | 點值: ${p.pointValue} | 損益: ${p.unrealized_pnl_usd:.2f}")

            await asyncio.sleep(3)

    except Exception as e:
        print(f"💥 測試過程中發生異常: {e}")
    finally:
        print("\n" + "="*40)
        print("🎉 強化測試流程結束。")

if __name__ == "__main__":
    try:
        asyncio.run(run_final_test())
    except KeyboardInterrupt:
        print("\n👋 測試由使用者手動終止。")