# ProjectX: TopstepX 自動化交易系統 (Windows 7 特化版)

![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%207%20%7C%2010%20%7C%2011-lightgrey)

這是一個專為 **TopstepX** 平台設計的量化交易系統。針對 **Windows 7** 不支援穩定 WebSocket 的環境限制，本系統採用了高效能的 **非同步輪詢 (Async Polling)** 架構，確保在舊版系統上也能實現穩定的行情追蹤、損益監控與自動下單。

---

## 🏗️ 系統架構說明 (System Architecture)

本系統模擬「全自動化餐廳」的運作模式，將複雜的 API 請求拆解為多個獨立角色，並透過「中央白板」共享資訊。



### 核心組件角色：
* **中央白板 (DataHub)**: 所有數據的集散地，儲存最新 K 線、持倉與帳戶餘額。
* **行情採購員 (Market Poller)**: 每秒輪詢一次 K 線，確保「未收盤 K 線」的價格能即時更新至白板。
* **帳務員 (Order Monitor)**: 每 2 秒同步一次帳戶狀態，檢查掛單是否成交，並美化輸出監控面板。
* **執行廚師 (Trading Service)**: 下單執行官，支援自動計算方向的止盈止損 (Bracket Orders)。
* **店經理 (Risk Manager)**: 風控防火牆，當日損益低於限制（如 -$1000）時自動觸發停機保護。

---

## 📊 資料運作流程 (Data Flow)

描述一個交易信號從觸發到執行的完整生命週期：



```mermaid
sequenceDiagram
    participant API as TopstepX 伺服器
    participant Poller as Market Poller (行情)
    participant Hub as DataHub (中央白板)
    participant Strategy as 交易策略 (Logic)
    participant Chef as Trading Service (執行)

    loop 每 1-2 秒
        Poller->>API: 請求最新 K 線 (includePartial)
        API-->>Poller: 回傳價格數據
        Poller->>Hub: 更新白板上的現價與 K 線
    end

    Note over Strategy, Hub: 策略偵測到價格滿足條件
    Strategy->>Chef: 指令：買入 1 口 MNQ
    Chef->>API: POST /api/Order/place (帶 SL/TP)
    API-->>Chef: 回傳 OrderID #999
    Chef->>Hub: 登記掛單編號

    📂 資料夾與檔案說明目錄/檔案說明core/系統地基。包含 HTTP 客戶端 (client.py)、全域配置 (config.py) 與數據中心 (data_hub.py)。models/資料結構。使用 Pydantic 定義帳戶、合約、訂單與 K 線的模型，確保數據正確性。services/功能實現。行情輪詢、訂單監控、下單邏輯與風控檢查都在這裡。utils/輔助工具。針對 Win7 最佳化的日誌系統 (logger.py) 與 Telegram 通知。strategies/交易大腦。存放使用者自定義的交易策略邏輯。.env私密憑證。儲存你的 API Key、帳號 ID 與機器人 Token。main.py啟動入口。協調整合所有服務並開始運行的主程式。