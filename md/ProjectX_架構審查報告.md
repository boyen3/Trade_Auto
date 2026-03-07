# ProjectX 架構審查報告
**TopstepX 自動化交易系統 · 完整審查**
審查日期：2026-03-03

---

## 一、系統全貌

```
main.py (ProjectXEngine)
│
├── core/
│   ├── TopstepXClient      # REST HTTP 客戶端 (httpx async)
│   ├── RealTimeClient      # SignalR 即時推播 (threading)
│   ├── DataHub             # 全域數據中心 (class-level 靜態)
│   ├── EventEngine         # 策略事件驅動 (0.5s polling)
│   ├── StateStore          # JSON 持久化狀態
│   └── Config / Constants / Exceptions
│
├── models/
│   ├── AccountModel        # Pydantic，alias 對應 API 欄位
│   ├── BarModel / ContractModel
│   └── PositionModel / OrderResponse / TradeModel
│
├── services/
│   ├── AccountService      # 帳戶查詢
│   ├── MarketDataService   # K 線查詢 + 輪詢 (start_polling)
│   ├── MarketPoller        # K 線輪詢 (與上方重疊)
│   ├── TradingService      # 下單 / 撤單 / 查詢持倉
│   ├── OrderMonitor        # 帳戶 + 持倉同步監控
│   ├── RiskManager         # 每日最大虧損風控
│   └── TelegramService     # Telegram 通知
│
└── utils/
    └── logger              # 按日期滾動日誌檔
```

---

## 二、嚴重問題 🔴

### 2.1 雙引擎數據競爭 (Data Race)

**問題：**
`MarketPoller` 和 `OrderMonitor` 同時以 asyncio Task 方式並發執行，
兩者都會修改同一個 `PositionModel` 物件的 `currentPrice` 欄位：

```python
# market_poller.py
pos = DataHub.get_position(symbol)
if pos: pos.currentPrice = last_price   # ← 修改同一物件

# order_monitor.py（同時執行）
pos.currentPrice = latest_bars['c'].iloc[-1]  # ← 也在修改
```

雖然 asyncio 是協作式，不會有真正的 thread-level race，
但兩個 coroutine 的更新邏輯互相覆蓋，導致哪個數字最終生效是不確定的。

**建議修正：**
指定單一負責人。`currentPrice` 的更新應只由 `MarketPoller` 負責；
`OrderMonitor` 讀取後計算，不修改物件。

---

### 2.2 RiskManager 浪費 API 額度

**問題：**
每次呼叫 `check_safety()` 都重新打 API 取得持倉：

```python
# risk_manager.py
positions = await self.ts.get_open_positions(account_id)
```

但 `OrderMonitor` 已經在每 2 秒同步持倉至 `DataHub`，等於同樣的數據
打了兩次 API。在 TopstepX 有頻率限制的情況下，這會加速觸發 429。

**建議修正：**
```python
# 改為直接讀 DataHub，零 API 消耗
positions = list(DataHub.positions.values())
total_pnl = sum(p.unrealized_pnl_usd for p in positions)
```

---

### 2.3 main.py 策略邏輯硬編碼

**問題：**
`ProjectXEngine.run()` 直接包含「無持倉就進場」的交易判斷，
且參數（account_id、symbol、sl/tp）全部寫死在 `__init__` 的 dict 裡：

```python
self.config = {
    "account_id": 18699057,   # 硬編碼真實帳號 ID
    "symbol": "CON.F.US.MNQ.H26",
    ...
}
```

這讓 `main.py` 同時承擔「系統協調」和「策略執行」兩個職責，
違背了 `strategies/base_strategy.py` 所設計的策略分離原則。

**建議修正：**
將進場邏輯移至 `strategies/` 資料夾，`main.py` 只負責啟動各服務，
再透過 `EventEngine` 將 `ON_BAR` 事件派發給策略。

---

## 三、中等問題 🟡

### 3.1 MarketDataService 與 MarketPoller 職責重疊

`MarketDataService` 有 `start_polling()` 方法，
`MarketPoller` 是獨立 class 也做同一件事。
`main.py` 目前呼叫的是 `market_service.start_polling()`（MarketDataService 版本），
`MarketPoller` 反而從未被使用。

**建議：** 刪除 `MarketDataService.start_polling()`，統一使用 `MarketPoller`，
職責更清晰，後續要加多商品監控也更容易。

---

### 3.2 OrderMonitor 繞過 AccountService

```python
# order_monitor.py - 直接呼叫 client，且 payload 是空 {}
acc_data = await self.ts.client.request("POST", "/api/Account/search", json={})
```

正確的 payload 應是 `{"onlyActiveAccounts": True}`（參見 AccountService），
空 payload 可能回傳已停用帳戶，導致選到錯誤帳戶。

**建議：** 注入 `AccountService` 並呼叫 `get_primary_account()`。

---

### 3.3 RealTimeClient 從未被整合

`rt_client.py` 是完整的 SignalR 即時推播實作，但 `main.py` 和所有 services
都沒有使用它。系統目前完全依賴輪詢（polling）。

這在 README 說明「Win7 不支援穩定 WebSocket 故改用輪詢」，
**若部署在 Win10/11，應啟用 RealTimeClient 替代輪詢以降低延遲與 API 消耗。**

---

### 3.4 PositionModel.pointValue 預設值風險

```python
pointValue: float = 2.0  # 預設值，由 Monitor 自動從合約資訊補齊
```

如果合約資訊補齊流程失敗（網路異常、合約 ID 不在快取），
`pointValue` 會靜默保持 `2.0`，導致損益計算錯誤但不拋出任何警告。

**建議：** 改為 `Optional[float] = None`，在 `unrealized_pnl_usd` 裡：
```python
if self.pointValue is None:
    return 0.0  # 明確標示「尚未校正」
```

---

### 3.5 StateStore 高頻 I/O

每次呼叫 `update_strategy_state()` 或 `update_global_state()` 都立刻寫磁碟。
若策略頻繁更新狀態，在老舊硬碟的 Win7 機器上可能成為瓶頸。

**建議：** 改為「延遲寫入」（dirty flag + 定期 flush）：
```python
self._dirty = True  # 標記需要寫入
# 由獨立 task 每 30 秒 flush 一次
```

---

## 四、小建議 🟢

| 位置 | 問題 | 建議 |
|------|------|------|
| `utils/logger.py` | `logs/` 目錄用相對路徑建立 | 改用 `Path(__file__).parent.parent / "logs"` |
| `utils/logger.py` | Console/File handler 無 level 差異 | Console 設 INFO，File 設 DEBUG |
| `services/risk_manager.py` | `MAX_LOSS = 1000.0` 硬編碼 | 移至 `.env` → `Config.MAX_DAILY_LOSS` |
| `models/market.py` | `BarModel` 欄位名太短 (t/o/h/l/c/v) | 加 `Field(alias=...)` 提升可讀性 |
| `requirements.txt` | 缺少版本鎖定 | 改用 `httpx==0.27.0` 等，避免升版破壞 |
| `requirements.txt` | 缺少 `signalrcore` | `rt_client.py` 依賴但未列入 |

---

## 五、優先修正順序

```
高優先
  1. RiskManager 改讀 DataHub（避免浪費 API 額度）
  2. currentPrice 更新邏輯統一由 MarketPoller 負責
  3. main.py 策略邏輯分離至 strategies/

中優先
  4. OrderMonitor 改用 AccountService（修正空 payload bug）
  5. 刪除 MarketDataService.start_polling()，統一用 MarketPoller
  6. PositionModel.pointValue 改為 Optional[float] = None

低優先
  7. StateStore 延遲寫入
  8. Logger 路徑與 level 調整
  9. MAX_LOSS 移至設定檔
 10. requirements.txt 版本鎖定
```

---

## 六、總體評估

這個系統的架構設計思路是清晰且正確的：

- **分層清楚**：core / models / services / strategies 職責分明
- **錯誤處理完整**：retry 機制、429 退避、JSON 解析防禦都有考慮
- **Win7 相容設計合理**：用輪詢替代不穩定的 WebSocket 是務實的取捨
- **Pydantic 使用正確**：alias 對應、預設值防禦都有

主要的改進空間集中在「服務間協調」：部分服務之間互相不知道對方的存在，
導致重複打 API、數據更新責任不清。建立「單一數據寫入者」原則，
讓每個欄位只有一個服務負責寫入，其他服務只讀，可以解決大部分問題。
