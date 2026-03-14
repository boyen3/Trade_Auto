# core/event_engine.py
import asyncio
from typing import Callable, List, Dict, Optional
from core.data_hub import DataHub
from utils.logger import logger


class EventEngine:
    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {
            "ON_BAR":   [],
            "ON_TICK":  [],
            "ON_ORDER": []
        }
        self._is_running = False
        self._last_bar_time = {}
        self._initial_fired = set()  # 記錄哪些 symbol 已完成初始觸發
        self.bar_store = None        # 由 main.py 注入：engine.event_engine.bar_store = engine.bar_store

    def register(self, event_type: str, handler: Callable):
        if event_type in self._handlers:
            self._handlers[event_type].append(handler)

    async def start(self):
        self._is_running = True
        logger.info("⚙️ 虛擬事件引擎已啟動 - 正在監控 DataHub 狀態...")

        # 等待 DataHub 有 K 棒數據（最多 60 秒）
        for i in range(60):
            if DataHub.bars:
                break
            await asyncio.sleep(1)
        else:
            logger.warning("⚙️ EventEngine：等待 DataHub K 棒超時（60秒），繼續監控...")

        while self._is_running:
            for symbol, df in DataHub.bars.items():
                if df.empty:
                    continue

                current_bar_time = df.index[-1]

                # 第一次看到這個 symbol → 用 BarStore 完整歷史觸發一次
                if symbol not in self._last_bar_time:
                    self._last_bar_time[symbol] = current_bar_time
                    if symbol not in self._initial_fired:
                        self._initial_fired.add(symbol)

                        # 優先用 BarStore 完整歷史（SR 識別需要 500+ 根）
                        # 策略用 5M K 棒，從 BarStore 讀完整 5M 歷史
                        full_df = df  # fallback
                        if self.bar_store is not None:
                            try:
                                loaded = self.bar_store.load(symbol, "5m", limit=1800)
                                if loaded is not None and len(loaded) > len(df):
                                    full_df = loaded
                            except Exception as e:
                                logger.warning(f"⚙️ BarStore 讀取失敗，使用 DataHub 數據: {e}")

                        logger.info(f"⚙️ 初始觸發 ON_BAR: {symbol} ({len(full_df)} 根K棒，含完整歷史)")
                        await self._emit("ON_BAR", symbol, full_df)
                    continue

                # 之後每次有新 K 棒才觸發
                if current_bar_time > self._last_bar_time[symbol]:
                    self._last_bar_time[symbol] = current_bar_time
                    # 常規觸發也用完整歷史（讓 SR 計算有足夠 context）
                    full_df = df
                    if self.bar_store is not None:
                        try:
                            loaded = self.bar_store.load(symbol, "5m", limit=1800)
                            if loaded is not None and len(loaded) > len(df):
                                full_df = loaded
                        except Exception:
                            pass
                    await self._emit("ON_BAR", symbol, full_df)

            await asyncio.sleep(0.5)

    async def _emit(self, event_type: str, *args, **kwargs):
        tasks = [handler(*args, **kwargs) for handler in self._handlers[event_type]]
        if tasks:
            await asyncio.gather(*tasks)

    def stop(self):
        self._is_running = False
