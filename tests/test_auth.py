import asyncio
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.client import TopstepXClient
from services.account import AccountService

async def run_test():
    print(f"--- [自動帳戶選取測試] ---")
    client = TopstepXClient()
    account_service = AccountService(client)
    
    try:
        # 使用我們新寫的自動選取邏輯
        account = await account_service.get_primary_account()
        
        if account:
            print(f"✅ 成功鎖定目標帳戶：")
            print(f"   名稱: {account.name}")
            print(f"   ID:   {account.id}")
            print(f"   餘額: ${account.balance:,.2f}")
            
            # 未來這裡可以接續開發：
            # await trading_service.set_target_account(account.id)
        else:
            print("❌ 無法獲取任何可用帳戶。")

    except Exception as e:
        print(f"❌ 測試發生錯誤: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_test())
