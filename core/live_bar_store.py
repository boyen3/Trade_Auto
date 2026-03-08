"""
live_bar_store.py
即時模式的 BarStore 包裝器，讓 SNRStrategyV3 在即時交易時
可以透過 bar_store.load() 取得任何時框的 K 棒數據。

放置路徑：E:\Trade_Auto\core\live_bar_store.py
"""
import asyncio
import pandas as pd
from datetime import datetime
from typing import Optional
from utils.logger import logger
from core.constants import BarUnit


# 時框字串 → (BarUnit, unitNumber, 建議 limit)
TF_MAP = {
    "1m":  (BarUnit.MINUTE, 1,   200),
    "5m":  (BarUnit.MINUTE, 5,   200),
    "15m": (BarUnit.MINUTE, 15,  200),
    "1h":  (BarUnit.HOUR,   1,   200),
    "4h":  (BarUnit.HOUR,   4,   200),
    "1d":  (BarUnit.DAY,    1,   200),
}


class LiveBarStore:
    """
    即時模式下模擬 BacktestBarStore 的介面。
    策略呼叫 bar_store.load(contract_id, timeframe, limit) 時，
    直接向 TopstepX API 請求對應時框的 K 棒數據。

    使用快取避免短時間內重複呼叫 API：
    同一時框在 cache_seconds 秒內只請求一次。
    """

    def __init__(self, market_service, cache_seconds: int = 60):
        self.ms            = market_service
        self.cache_seconds = cache_seconds
        self._cache: dict  = {}          # key: (contract_id, timeframe) → (timestamp, df)
        self.current_bar_time    = None  # 由策略/引擎更新
        self.current_bar_time_ns = None

    def load(self, contract_id: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        """
        同步介面（策略內部呼叫）。
        在 asyncio event loop 裡用 run_coroutine_threadsafe 或直接 await，
        這裡用 asyncio.get_event_loop().run_until_complete 包裝。
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在 async 環境中，直接建立 task 並等待
                import concurrent.futures
                future = asyncio.ensure_future(
                    self._load_async(contract_id, timeframe, limit)
                )
                # 用 run_until_complete 不行（loop 已在跑），改用 thread executor
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        self._load_async(contract_id, timeframe, limit)
                    ).result(timeout=10)
                return result
            else:
                return loop.run_until_complete(
                    self._load_async(contract_id, timeframe, limit)
                )
        except Exception as e:
            logger.error(f"[LiveBarStore] load 失敗 {timeframe}: {e}")
            return pd.DataFrame()

    async def load_async(self, contract_id: str, timeframe: str,
                         limit: int = 200) -> pd.DataFrame:
        """async 版本，在 async 環境中直接 await。"""
        return await self._load_async(contract_id, timeframe, limit)

    async def _load_async(self, contract_id: str, timeframe: str,
                          limit: int = 200) -> pd.DataFrame:
        cache_key = (contract_id, timeframe)
        now       = datetime.utcnow().timestamp()

        # 快取命中
        if cache_key in self._cache:
            ts, df = self._cache[cache_key]
            if now - ts < self.cache_seconds:
                return df.tail(limit).copy()

        # 查時框對應
        tf_info = TF_MAP.get(timeframe)
        if tf_info is None:
            logger.warning(f"[LiveBarStore] 不支援的時框: {timeframe}")
            return pd.DataFrame()

        bar_unit, unit_number, _ = tf_info

        try:
            from datetime import timedelta
            end_time   = datetime.utcnow()
            # 根據時框決定往回取多久
            days_back  = {
                "1m": 1, "5m": 3, "15m": 7,
                "1h": 30, "4h": 90, "1d": 400
            }.get(timeframe, 30)
            start_time = end_time - timedelta(days=days_back)

            payload = {
                "contractId":      str(contract_id),
                "live":            False,
                "startTime":       start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime":         end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "unit":            bar_unit.value,
                "unitNumber":      unit_number,
                "limit":           limit,
                "includePartialBar": False,
            }

            data = await self.ms.client.request(
                "POST", "/api/History/retrieveBars", json=payload
            )
            bars = data.get("bars", [])
            if not bars:
                return pd.DataFrame()

            from models.market import BarModel
            df = pd.DataFrame([BarModel(**b).dict() for b in bars])
            df.set_index('t', inplace=True)
            df.sort_index(inplace=True)

            # 確保欄位名稱一致（o/h/l/c/v）
            col_map = {'open':'o','high':'h','low':'l','close':'c','volume':'v'}
            df.rename(columns=col_map, inplace=True)

            self._cache[cache_key] = (now, df)
            logger.debug(f"[LiveBarStore] {timeframe} 載入 {len(df)} 根")
            return df.tail(limit).copy()

        except Exception as e:
            logger.error(f"[LiveBarStore] API 請求失敗 {timeframe}: {e}")
            return pd.DataFrame()

    def invalidate(self, timeframe: str = None):
        """清除快取（換合約或手動刷新時用）"""
        if timeframe:
            keys = [k for k in self._cache if k[1] == timeframe]
            for k in keys:
                del self._cache[k]
        else:
            self._cache.clear()
