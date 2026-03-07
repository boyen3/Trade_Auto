import asyncio
from services.account import AccountService
from services.market_data import MarketDataService
from services.trading import TradingService
from core.client import TopstepXClient

async def main():
    client = TopstepXClient()
    trade_service = TradingService(client)
    acc_service = AccountService(client)
    
    await client.login()
    account = await acc_service.get_primary_account()
    
    # 步驟 1: 進場 (若要 100 點，請輸入 400 ticks)
    # ... (省略進場與減倉代碼) ...

    # 步驟 3: 實際執行 A & B
    orders = await trade_service.get_open_orders(account.id)
    for o in orders:
        if o['type'] == 1: # Take Profit
            print(f"🔥 執行步驟 A: 取消止盈單 {o['id']}")
            await trade_service.cancel_order(account.id, o['id'])
        if o['type'] == 4: # Stop Loss
            new_stop = o['stopPrice'] + 5.0 # 往上移 5 點
            print(f"🛠️ 執行步驟 B: 修改止損單 {o['id']} 至 {new_stop}")
            await trade_service.modify_order(account.id, o['id'], stopPrice=new_stop)

if __name__ == "__main__":
    asyncio.run(main())