# services/market_data.py
import asyncio
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Optional
from core.client import TopstepXClient
from core.constants import BarUnit
from core.data_hub import DataHub
from models.market import BarModel, ContractModel
from utils.logger import logger


class MarketDataService:
    def __init__(self, client: TopstepXClient):
        self.client = client
        self._contract_cache = {}
        self._is_polling = False
        self.bar_store = None   # 由 main.py 注入：self.market_service.bar_store = bar_store

    # ──────────────────────────────────────────
    # 行情輪詢
    # ──────────────────────────────────────────

    async def start_polling(self, contract_id: str, interval: float = 30.0):
        """
        啟動行情輪詢，每 30 秒執行一次：
          1. 拉最新 15 分 K（100 根）→ 更新 DataHub 供策略使用
          2. 拉最新 5 分 K（50 根）  → 僅更新 BarStore 存檔
          3. 若 bar_store 已注入，增量寫入本地 Parquet
        """
        self._is_polling = True
        logger.info(f"📈 行情追蹤啟動: {contract_id} | 間隔:30s")
        while self._is_polling:
            try:
                # 策略主時框：5M K → DataHub（每根 5M K 棒觸發 EventEngine）
                df_5m = await self.get_bars_df(
                    contract_id, unit=BarUnit.MINUTE, unit_number=5, limit=100
                )
                if not df_5m.empty:
                    DataHub.update_bars(contract_id, df_5m)
                    if self.bar_store:
                        self.bar_store.append_latest(contract_id, df_5m, "5m")

                # 15M / 1H / 4H → 僅存檔（供策略讀取，不觸發 EventEngine）
                # FIX #6: 1H 至少要 150 根供 EMA50 計算，4H 至少 55 根
                #         原本 1H=30, 4H=10 嚴重不足，HTF 趨勢永遠 neutral
                if self.bar_store:
                    for tf, unit, unit_num, lim in [
                        ("15m", BarUnit.MINUTE, 15, 100),
                        ("1h",  BarUnit.HOUR,   1,  200),
                        ("4h",  BarUnit.HOUR,   4,  100),
                    ]:
                        try:
                            df_tf = await self.get_bars_df(
                                contract_id, unit=unit, unit_number=unit_num, limit=lim
                            )
                            if not df_tf.empty:
                                self.bar_store.append_latest(contract_id, df_tf, tf)
                        except Exception:
                            pass

            except Exception as e:
                logger.error(f"⚠️ 行情抓取異常: {e}")
            await asyncio.sleep(interval)

    def stop_polling(self):
        self._is_polling = False

    # ──────────────────────────────────────────
    # 合約查詢
    # ──────────────────────────────────────────

    async def get_contract_info(self, contract_id: str) -> Optional[ContractModel]:
        if contract_id in self._contract_cache:
            return self._contract_cache[contract_id]
        payload = {"contractId": contract_id}
        data = await self.client.request("POST", "/api/Contract/searchById", json=payload)
        if data.get("success") and data.get("contract"):
            contract = ContractModel(**data["contract"])
            self._contract_cache[contract_id] = contract
            return contract
        return None

    async def search_contract(self, query: str, live: bool = False) -> List[ContractModel]:
        payload = {"live": live, "searchText": query}
        data = await self.client.request("POST", "/api/Contract/search", json=payload)
        return [ContractModel(**c) for c in data.get("contracts", [])]

    # ──────────────────────────────────────────
    # K 棒拉取
    # ──────────────────────────────────────────

    async def get_bars_df(self, contract_id: str,
                          unit: BarUnit = BarUnit.MINUTE,
                          unit_number: int = 1,
                          limit: int = 100,
                          start_time: Optional[datetime] = None,
                          end_time: Optional[datetime] = None) -> pd.DataFrame:
        """
        K 線獲取。
        :param unit_number:  時間框架（1=1分, 5=5分, 15=15分...）
        :param limit:        最多取幾根（API 上限 20000）
        :param start_time:   起始時間（BarStore 補拉缺口用）
        :param end_time:     結束時間（BarStore 補拉缺口用）
        """
        if end_time is None:
            end_time = datetime.utcnow()
        if start_time is None:
            days_back = max(3, int(limit * unit_number / 1440) + 1)
            start_time = end_time - timedelta(days=days_back)

        payload = {
            "contractId": str(contract_id),
            "live": False,
            "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit.value,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": True
        }
        data = await self.client.request("POST", "/api/History/retrieveBars", json=payload)
        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame()

        df = pd.DataFrame([BarModel(**b).dict() for b in bars])
        if not df.empty:
            df.set_index('t', inplace=True)
            df.sort_index(inplace=True)
        return df

    async def preload_all_contracts(self):
        logger.info("⏳ 正在預載全商品規格以校正損益計算...")
        pass
