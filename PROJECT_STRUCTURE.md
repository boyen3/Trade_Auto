# ProjectX — 專案結構說明

> **系統**：TopstepX 自動化交易系統（Windows 7 特化版）
> **交易標的**：MNQ（微型那斯達克期貨，合約代碼 CON.F.US.MNQ.H26）
> **架構**：非同步輪詢（Async Polling），Python 3.8，asyncio
> **更新日期**：2026-03-09

---

## 系統架構總覽

```
ProjectXEngine（main.py）
│
├── core/TopstepXClient          # HTTP 客戶端（httpx async）
├── core/DataHub                 # 全域數據中心（class-level 靜態）
├── core/EventEngine             # 事件驅動，每 0.5s 觸發 ON_BAR
│
├── services/MarketDataService   # K 線查詢 + 30s 輪詢
├── services/BarStore            # 本地 Parquet 歷史數據（補拉 + 增量寫入）
├── services/MarketPoller        # 即時現價輪詢（更新 DataHub）
├── services/OrderMonitor        # 每 2s 同步帳戶 + 持倉至 DataHub
├── services/TradingService      # 下單 / 撤單 / 平倉（Bracket Order）
├── services/RiskManager         # 風控防火牆
├── services/TelegramService     # Telegram 通知 + Bot 指令
├── services/DailyReportService  # 每日交易報表
├── services/Dashboard           # Console 即時儀表板（每 5s 刷新）
│
└── strategies/SNRStrategyV3     # 目前主策略（5M 假突破 × 多時框 EMA 趨勢）
```

---

## 資料流

```
TopstepX API
    ↓ MarketDataService.start_polling()（每 30 秒拉 K 線）
    ↓ BarStore.append_latest()（增量寫入本地 Parquet）
    ↓ DataHub.update_bars()
    ↓ EventEngine 觸發 ON_BAR（每 0.5s 檢查是否有新 K 棒）
    ↓ SNRStrategyV3.on_bar(symbol, df)
    ↓ TradingService.place_order()（Bracket Order：含 SL/TP 子單）
    ↓ TopstepX API
```

---

## 完整目錄結構

```
E:\Trade_Auto\
│
│  main.py              ← 主程式入口（BarStore + SNRStrategyV3，生產用）
│  main_v3.py           ← 備用主程式（LiveBarStore + C 高頻參數）
│  test_v3.py           ← SNR v3 快速測試腳本
│  requirements.txt     ← 套件清單（httpx / pydantic / pandas / talib 等）
│  .gitignore           ← 忽略 .env / logs / data / __pycache__ / tools
│  read.md              ← 系統 README
│
├─ core/                ← 系統地基（不依賴 services / strategies）
│     client.py         ← TopstepXClient：登入 / Token 自動刷新 / 重試 / 429 退避
│     config.py         ← 從 .env 讀取 USERNAME / API_KEY / BASE_URL / ACCOUNT_ID
│     constants.py      ← OrderSide / OrderType / BracketType / BarUnit 枚舉
│     data_hub.py       ← DataHub：全域靜態數據中心（bars / positions / account_info）
│     event_engine.py   ← EventEngine：0.5s polling，觸發 ON_BAR 事件給策略
│     exceptions.py     ← 自訂例外：TopstepXError / AuthError / RateLimitError
│     live_bar_store.py ← LiveBarStore：即時模式 BarStore 包裝器（main_v3 使用）
│     rt_client.py      ← RealTimeClient：SignalR 即時推播（備用，目前未整合）
│     state_store.py    ← StateStore：JSON 持久化（風控計數 / 策略記憶跨重啟）
│
├─ models/              ← Pydantic 資料模型（API 欄位對應）
│     account.py        ← AccountModel（id / name / balance / realizedPnL）
│     market.py         ← BarModel（t/o/h/l/c/v）/ ContractModel（tickSize / tickValue）
│     order.py          ← OrderResponse / PositionModel / TradeModel
│
├─ services/            ← 功能服務層
│     account.py        ← AccountService：帳戶查詢 / 自動選取主要帳戶
│     bar_store.py      ← BarStore：本地 Parquet（5m/15m/1h/4h/1d/1w），補拉缺口
│     bot_handler.py    ← BotCommandHandler：Telegram 指令處理
│                           /status /pause /resume /close /risk /diag
│     daily_report.py   ← DailyReportService：每日損益報表，定時發送 Telegram
│     dashboard.py      ← Dashboard：Console 即時儀表板（帳戶 / 趨勢 / SR 區）
│     market_data.py    ← MarketDataService：K 線查詢 / 合約查詢 / 30s 輪詢
│     market_poller.py  ← MarketPoller：高頻現價輪詢，更新 DataHub currentPrice
│     notification.py   ← TelegramService：Bot 通知 / 指令接收 / 各類模板訊息
│     order_monitor.py  ← OrderMonitor：每 2s 同步帳戶 + 持倉 + 已實現損益
│     risk_manager.py   ← RiskManager：風控檢查（從 .env 讀取所有參數）
│                           - MAX_DAILY_LOSS / MAX_DAILY_TRADES
│                           - MAX_CONSECUTIVE_LOSSES / MAX_POSITION_SIZE
│                           - 收盤時間 / 強制平倉時間
│     trading.py        ← TradingService：下單 / 撤單 / 查詢持倉 / 今日成交
│
├─ strategies/          ← 交易策略
│     base_strategy.py  ← BaseStrategy（抽象基底）：on_bar / run_step / save_memory
│     snr_strategy_v3.py← ★ 目前主策略（SNR v3）
│                           - 時間框：5M 進場，1H/4H EMA 趨勢過濾
│                           - SR 識別：密度法（多時框合併）
│                           - 進場條件：假突破 + 動能衰減 + 可選 Pin Bar
│                           - 風控：0.8R 部分止盈 50%，1.2R 啟動移動止盈
│     snr_strategy.py   ← SNR v2（舊版，已被 v3 取代，備用參考）
│                           - 分型法 SR + 流動性掠奪 + ADX 過濾 + 5M 確認
│     smc_strategy.py   ← SMC 策略（Order Block + FVG，備用）
│     sma_cross.py      ← SMA 均線交叉策略（基礎範例）
│     base_strategy.py  ← 策略基底類別
│     unattended_test.py← 無人值守測試（33 項：風控 / API / Bot 全覆蓋）
│     api_coverage_test.py ← API 端點覆蓋測試
│     rapid_test.py     ← 快速冒煙測試
│     speed_test_5m.py  ← 5M 回測速度壓力測試
│     stress_test.py    ← 系統壓力測試
│     bk/               ← 策略備份壓縮檔
│
├─ backtest/            ← 回測引擎
│     engine.py         ← SNRBacktestRunner：直接使用 snr_strategy_v3，零複製
│                           - BacktestBarStore（高速 Parquet 預載）
│                           - BacktestTradingService（模擬下單）
│                           - SL/TP 掃描（numpy 高速版）
│                           - HTF EMA 預算查表（加速 10-50 倍）
│                           - param_scan()：單參數掃描工具
│                           資料來源：data_backtest/*.parquet
│
├─ tests/               ← 測試腳本
│     final_logic_test.py   ← 全系統邏輯強化測試
│     full_system_test.py   ← 全系統集成測試
│     regression_test.py    ← 回歸測試
│     test_advanced.py      ← 進階場景測試（加倉 / 移動止損 / 跨合約）
│     test_auth.py          ← 帳戶認證測試
│     test_auto_stop.py     ← 自動止損同步測試
│     test_complex_logic.py ← 複雜邏輯測試
│     test_final_logic.py   ← 最終邏輯驗證
│     test_market.py        ← 市場數據測試
│     test_signalr.py       ← SignalR 連線測試
│     test_trading.py       ← 下單同步測試
│     demo_scenario.py      ← 示範場景腳本
│
├─ tools/               ← 分析工具（在 .gitignore 中，不 sync 至 GitHub）
│     backtest_engine.py    ← 舊版回測引擎
│     backtest_grid.py      ← 網格參數搜索
│     backtest_scan.py      ← 參數掃描
│     backtest_wf.py        ← Walk-Forward 測試
│     market_profile.py/2/3 ← 市場輪廓分析
│     vwap_analysis.py/2    ← VWAP 分析
│     debug_htf.py          ← HTF 趨勢除錯工具
│     convert_nq_data.py    ← NQ 數據格式轉換
│     topstep_sim.py/2      ← TopstepX 規則模擬器
│     （其他分析 / 測試工具）
│
├─ utils/
│     logger.py         ← 雙軌 Logger（INFO → _info.log / WARNING → _error.log）
│
├─ data/                ← 即時交易歷史數據（.gitignore，不 sync）
│     CON_F_US_MNQ_H26_5m.parquet
│     CON_F_US_MNQ_H26_15m.parquet
│     CON_F_US_MNQ_H26_1h.parquet
│     CON_F_US_MNQ_H26_4h.parquet
│     CON_F_US_MNQ_H26_1d.parquet
│     CON_F_US_MNQ_H26_1w.parquet
│     system_state.json ← StateStore 持久化狀態
│
├─ data_backtest/       ← 回測歷史數據（.gitignore，不 sync）
│     CON_F_US_MNQ_H26_1m.parquet
│     CON_F_US_MNQ_H26_5m.parquet
│     CON_F_US_MNQ_H26_15m.parquet
│     CON_F_US_MNQ_H26_1h.parquet
│     CON_F_US_MNQ_H26_4h.parquet
│     CON_F_US_MNQ_H26_1d.parquet
│
├─ logs/                ← 日誌（.gitignore，不 sync）
│     YYYY-MM-DD_info.log
│     YYYY-MM-DD_error.log
│
└─ md/                  ← 文件
      ProjectX_架構審查報告.md  ← 完整架構審查（問題清單 + 修正建議）
      strategy_spec.md          ← SNR v3 策略規格說明
```

---

## 主要模組依賴關係

```
main.py
 ├── core/client.py          ← 所有 services 都依賴
 ├── core/data_hub.py        ← 所有 services 和 strategies 讀取
 ├── core/event_engine.py    ← 觸發 strategies
 ├── services/market_data.py
 │    └── services/bar_store.py
 ├── services/order_monitor.py
 ├── services/trading.py
 ├── services/risk_manager.py
 ├── services/notification.py
 │    └── services/bot_handler.py
 ├── services/daily_report.py
 ├── services/dashboard.py
 └── strategies/snr_strategy_v3.py
      └── strategies/base_strategy.py
           ├── core/data_hub.py
           └── core/state_store.py
```

---

## 關鍵設定

| 項目 | 值 | 說明 |
|------|-----|------|
| 交易合約 | CON.F.US.MNQ.H26 | 微型那斯達克期貨 |
| Tick 大小 | 0.25 點 | MNQ 最小跳動 |
| 點值 | $2 / 點 | 1 口 MNQ |
| 主策略時框 | 5M | 進場 K 棒 |
| 趨勢過濾 | 1H + 4H EMA | 多時框方向確認 |
| 部分止盈 | 0.8R 平 50% | 剩餘啟動移動止盈 |
| 移動止盈觸發 | 1.2R | 追蹤 SR 支撐 |
| 風控參數 | 全部在 .env | MAX_DAILY_LOSS 等 |

---

## 啟動方式

```bash
# 生產環境（使用 BarStore 本地歷史數據）
python main.py

# 備用（LiveBarStore 即時拉取，C 高頻參數）
python main_v3.py

# 回測
python -m backtest.engine
```

---

## 注意事項

- `.env` 含真實 API Key，絕對不能 commit 至 Git
- `data/` 和 `data_backtest/` 為 Parquet 大檔，已加入 .gitignore
- `tools/` 為分析工具，已加入 .gitignore，不會 sync 至 GitHub
- `rt_client.py` 為 SignalR 即時推播實作，Win7 環境不穩定，目前系統改用輪詢
- 換月時需更新 `config["contract_expiry"]` 和 `config["symbol"]`
