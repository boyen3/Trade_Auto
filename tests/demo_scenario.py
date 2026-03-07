# tests/demo_scenario.py
import asyncio
from core.client import TopstepXClient
from services.trading import TradingService
from services.notification import TelegramService
from core.constants import OrderSide
from utils.logger import logger

async def run_demo():
    client = TopstepXClient()
    ts = TradingService(client)
    notifier = TelegramService()
    
    await client.login()
    acc_id = 18699057 
    symbol = "CON.F.US.MNQ.H26"

    await notifier.send_message("🏗️ *開始 ProjectX 全功能演示流程*")

    # 1. 限價掛單 (Type 1 = Limit)
    limit_id = await ts.place_order(acc_id, symbol, 1, OrderSide.BUY, 1, limit_price=24500)
    if limit_id: await notifier.send_message(f"📍 [限價單] 已掛出: {limit_id}")
    await asyncio.sleep(4)

    # 2. 取消掛單
    if limit_id:
        await ts.cancel_order(acc_id, limit_id)
        await notifier.send_message(f"🗑️ [撤單] 成功取消限價單")
    await asyncio.sleep(2)

    # 3. 市價進場 (Type 2 = Market)
    entry_id = await ts.place_order(acc_id, symbol, 2, OrderSide.BUY, 1)
    if entry_id: await notifier.send_trade_notification("Demo", "BUY MARKET", symbol, "現價", 1)
    await asyncio.sleep(4)

    # 4. 設置止損 (Type 4 = Stop)
    sl_id = await ts.place_order(acc_id, symbol, 4, OrderSide.SELL, 1, stop_price=24800)
    if sl_id: await notifier.send_message(f"🛡️ [止損] 初始防線已設於 24800")
    await asyncio.sleep(4)

    # 5. 平倉總結 (Type 2 = Market Sell)
    await ts.place_order(acc_id, symbol, 2, OrderSide.SELL, 1)
    await notifier.send_message("📊 *演示結束：所有測試單已清理完成*")

    await client.http_client.aclose() # 優雅關閉連線

if __name__ == "__main__":
    asyncio.run(run_demo())