import asyncio
import sys
import os
from datetime import datetime

# 確保可以匯入專案模組
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 匯入所有補強後的組件
from core.client import TopstepXClient
from services.market_data import MarketDataService
from services.trading import TradingService
from services.risk_manager import RiskManager
from utils.logger import logger

# Windows 7 相容性處理
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def run_integration_test():
    logger.info("========================================")
    logger.info("🚀 啟動全系統集成測試 (Full System Test)")
    logger.info("========================================")

    try:
        # 1. 客戶端初始化
        client = TopstepXClient()
        market_service = MarketDataService(client)
        trading_service = TradingService(client)
        trading_service.market_service = market_service  # 注入依賴
        risk_manager = RiskManager(trading_service)

        # 2. 測試合約預載 (修復損益驚悚數字)
        logger.info("步驟 1: 執行合約規格預載...")
        await market_service.preload_all_contracts()
        if not market_service._contract_cache:
            logger.error("❌ 合約預載失敗，快取為空！")
            return
        logger.info(f"✅ 成功預載 {len(market_service._contract_cache)} 個合約")

        # 3. 測試帳戶連線
        logger.info("步驟 2: 檢查帳戶連線狀態...")
        acc_data = await client.request("POST", "/api/Account/search", json={})
        if not acc_data:
            logger.error("❌ 無法取得帳戶列表")
            return
        
        # 取得第一個帳戶進行測試
        acc = acc_data[0] if isinstance(acc_data, list) else acc_data.get("accounts")[0]
        account_id = acc['id']
        logger.info(f"✅ 成功對接帳戶: {acc.get('name')} (ID: {account_id})")

        # 4. 測試風控與損益計算邏輯 (即便無持倉也應能運作)
        logger.info("步驟 3: 執行即時風控掃描...")
        is_safe = await risk_manager.check_safety(account_id)
        if is_safe:
            logger.info("✅ 風控檢查通過 (帳戶狀態安全)")
        else:
            logger.warning("⚠️ 觸發風控線，請檢查帳戶餘額或持倉")

        # 5. 測試日誌檔案生成
        log_path = f"logs/trade_{datetime.now().strftime('%Y-%m-%d')}.log"
        if os.path.exists(log_path):
            logger.info(f"✅ 日誌系統確認正常: {log_path}")
        else:
            logger.error("❌ 日誌檔案未生成")

        logger.info("========================================")
        logger.info("🎉 所有核心模組邏輯驗證完成！系統已就緒。")
        logger.info("========================================")

    except Exception as e:
        logger.critical(f"💥 測試中斷，偵測到嚴重錯誤: {str(e)}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(run_integration_test())