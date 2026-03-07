import asyncio
from core.client import TopstepXClient
from services.account import AccountService
from services.market_data import MarketDataService
from services.trading import TradingService

async def main():
    client = TopstepXClient()
    acc_service = AccountService(client)
    mkt_service = MarketDataService(client)
    trade_service = TradingService(client)

    try:
        if not await client.login():
            print("無法登入")
            return

        account = await acc_service.get_primary_account()
        
        # 1. MES 買入兩口 (100點 = 400 ticks)
        mes_search = await mkt_service.search_contract("MES")
        mes_id = mes_search[0].id
        print(f"🚀 [MES] 買入 2 口，設置 SL: 400 ticks, TP: 400 ticks")
        res = await trade_service.place_order(
            account_id=account.id, contract_id=mes_id,
            order_type=2, side=0, size=2, sl_ticks=400, tp_ticks=400
        )

        print("等待 30 秒後減倉...")
        await asyncio.sleep(30)

        # 2. 手動減倉一口
        print(f"🚀 [MES] 減倉賣出 1 口")
        await trade_service.place_order(
            account_id=account.id, contract_id=mes_id, order_type=2, side=1, size=1
        )

        # 3. 獲取掛單並執行 A (取消止盈) 與 B (移動止損)
        print(f"🚀 執行 A & B 邏輯...")
        orders = await trade_service.get_open_orders(account.id)
        
        for o in orders:
            if o['contractId'] == mes_id:
                if o['type'] == 1: # Limit (止盈)
                    print(f"🔥 取消止盈單 ID: {o['id']}")
                    await trade_service.cancel_order(account.id, o['id'])
                elif o['type'] == 4: # Stop (止損)
                    # 移動止損到現價附近 (假設往上移 5 點)
                    new_sl = o['stopPrice'] + 5.0
                    print(f"🛠️ 修改止損單 ID: {o['id']} 至價格: {new_sl}")
                    await trade_service.modify_order(account.id, o['id'], stopPrice=new_sl)

        await asyncio.sleep(5)

        # 4. MNQ 市價進場 10 口
        mnq_search = await mkt_service.search_contract("MNQ")
        print(f"🚀 [MNQ] 市價進場 10 口")
        await trade_service.place_order(
            account_id=account.id, contract_id=mnq_search[0].id,
            order_type=2, side=0, size=10
        )

    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())