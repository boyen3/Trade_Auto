# services/bar_store.py
"""
本地 K 棒歷史數據儲存服務
===========================
功能：
  1. 系統啟動時讀本地 Parquet，補拉 API 缺口，冷啟動只需幾秒
  2. 每次輪詢拿到新 K 棒自動增量寫入，永久累積
  3. 支援六個時間框：5m / 15m / 1h / 4h / 1d / 1w
  4. 換月時自動對應新合約，舊合約數據保留
  5. 回測可直接讀 Parquet 檔案，格式與 DataHub 完全相容

存檔位置：
  E:\Trade_Auto\data\
    CON.F.US.MNQ.H26_5m.parquet
    CON.F.US.MNQ.H26_15m.parquet
    CON.F.US.MNQ.H26_1h.parquet
    CON.F.US.MNQ.H26_4h.parquet
    CON.F.US.MNQ.H26_1d.parquet
    CON.F.US.MNQ.H26_1w.parquet

使用方式（main.py）：
  from services.bar_store import BarStore
  bar_store = BarStore(market_service=self.market_service)
  await bar_store.initialize(self.config["symbol"])   # 啟動時補拉缺口
  self.bar_store = bar_store

  # 在 market_data.py 的 start_polling 每次更新後呼叫：
  await bar_store.append_latest(contract_id, df, timeframe)

  # 取歷史數據供策略使用：
  df_15m = bar_store.load(contract_id, "15m", limit=500)
  df_4h  = bar_store.load(contract_id, "4h",  limit=800)
"""

import asyncio
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from core.constants import BarUnit
from utils.logger import logger

# ──────────────────────────────────────────
# 時間框設定
# ──────────────────────────────────────────

TIMEFRAME_CONFIG = {
    "5m":  {"unit": BarUnit.MINUTE, "unit_number": 5,  "fetch_days": 30,   "label": "5分K"},
    "15m": {"unit": BarUnit.MINUTE, "unit_number": 15, "fetch_days": 60,   "label": "15分K"},
    "1h":  {"unit": BarUnit.HOUR,   "unit_number": 1,  "fetch_days": 180,  "label": "1小時K"},
    "4h":  {"unit": BarUnit.HOUR,   "unit_number": 4,  "fetch_days": 365,  "label": "4小時K"},
    "1d":  {"unit": BarUnit.DAY,    "unit_number": 1,  "fetch_days": 730,  "label": "日K"},
    "1w":  {"unit": BarUnit.WEEK,   "unit_number": 1,  "fetch_days": 1825, "label": "週K"},
}

DATA_DIR = Path(__file__).parent.parent / "data"


class BarStore:

    def __init__(self, market_service, data_dir: Path = DATA_DIR):
        self.ms       = market_service
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, pd.DataFrame] = {}   # {cache_key: df}
        logger.info(f"[BarStore] 初始化 | 存檔目錄: {self.data_dir}")

    # ──────────────────────────────────────────
    # 啟動：補拉所有時間框的缺口
    # ──────────────────────────────────────────

    async def initialize(self, contract_id: str):
        """
        系統啟動時呼叫。
        每個時間框：讀本地 → 計算缺口 → 補拉 API → 合併存檔。
        """
        logger.info(f"[BarStore] 開始初始化歷史數據 | 合約: {contract_id}")
        for tf, cfg in TIMEFRAME_CONFIG.items():
            try:
                await self._initialize_timeframe(contract_id, tf, cfg)
            except Exception as e:
                logger.error(f"[BarStore] {tf} 初始化失敗: {e}")
        logger.info(f"[BarStore] 初始化完成")

    async def _initialize_timeframe(self, contract_id: str, tf: str, cfg: dict):
        path = self._path(contract_id, tf)

        # 讀本地存檔
        local_df = self._read_parquet(path)

        if local_df.empty:
            # 第一次：從頭拉 fetch_days 天
            logger.info(f"[BarStore] {cfg['label']} 無本地存檔，全量拉取 {cfg['fetch_days']} 天...")
            fetch_df = await self._fetch_range(
                contract_id, cfg,
                start=datetime.utcnow() - timedelta(days=cfg["fetch_days"]),
                end=datetime.utcnow()
            )
            if not fetch_df.empty:
                self._write_parquet(fetch_df, path)
                self._update_cache(contract_id, tf, fetch_df)
                logger.info(f"[BarStore] {cfg['label']} 拉取完成 | {len(fetch_df)} 根")
        else:
            # 有存檔：只補拉缺口
            last_ts = local_df.index.max()
            now     = datetime.now(timezone.utc)
            gap_hours = (now - last_ts.to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / 3600

            # 缺口門檻 = 1 根 K 棒的時間長度（小時）
            n = cfg["unit_number"]
            u = cfg["unit"]
            if u == BarUnit.MINUTE:
                bar_hours = n / 60        # 5m → 0.083h, 15m → 0.25h
            elif u == BarUnit.HOUR:
                bar_hours = n             # 1h → 1h, 4h → 4h（舊邏輯誤乘了60）
            elif u == BarUnit.DAY:
                bar_hours = n * 24        # 1d → 24h
            else:
                bar_hours = n * 168       # 1w → 168h

            if gap_hours > bar_hours:
                logger.info(f"[BarStore] {cfg['label']} 補拉缺口 | 上次: {last_ts} 缺口: {gap_hours:.1f}h")
                gap_df = await self._fetch_range(
                    contract_id, cfg,
                    start=last_ts.to_pydatetime() + timedelta(minutes=1),
                    end=datetime.utcnow()
                )
                if not gap_df.empty:
                    merged = self._merge(local_df, gap_df)
                    self._write_parquet(merged, path)
                    self._update_cache(contract_id, tf, merged)
                    logger.info(f"[BarStore] {cfg['label']} 補拉 {len(gap_df)} 根 | 總計 {len(merged)} 根")
                else:
                    self._update_cache(contract_id, tf, local_df)
                    logger.info(f"[BarStore] {cfg['label']} 無新數據 | 本地 {len(local_df)} 根")
            else:
                self._update_cache(contract_id, tf, local_df)
                logger.info(f"[BarStore] {cfg['label']} 數據最新 | 本地 {len(local_df)} 根 | 最新: {last_ts}")

    # ──────────────────────────────────────────
    # 增量更新（每次輪詢後呼叫）
    # ──────────────────────────────────────────

    def append_latest(self, contract_id: str, new_df: pd.DataFrame, tf: str):
        """
        接收 market_data 輪詢的最新 K 棒，增量寫入本地。
        不需要 async，寫 Parquet 很快（< 50ms）。
        """
        if new_df.empty:
            return

        path     = self._path(contract_id, tf)
        local_df = self._cache.get(f"{contract_id}_{tf}", pd.DataFrame())

        if local_df.empty:
            merged = new_df
        else:
            merged = self._merge(local_df, new_df)

        # 只在有新 K 棒時才寫檔
        if local_df.empty or len(merged) > len(local_df):
            self._write_parquet(merged, path)
            self._update_cache(contract_id, tf, merged)

    # ──────────────────────────────────────────
    # 讀取（供策略使用）
    # ──────────────────────────────────────────

    def load(self, contract_id: str, tf: str,
             limit: Optional[int] = None,
             start: Optional[datetime] = None,
             end: Optional[datetime] = None) -> pd.DataFrame:
        """
        讀取歷史數據。
        優先從記憶體快取讀取，快取未命中才讀 Parquet。

        :param limit:  取最新 N 根
        :param start:  起始時間（回測用）
        :param end:    結束時間（回測用）
        """
        cache_key = f"{contract_id}_{tf}"
        df = self._cache.get(cache_key)

        if df is None or df.empty:
            path = self._path(contract_id, tf)
            df   = self._read_parquet(path)
            if not df.empty:
                self._update_cache(contract_id, tf, df)

        if df is None or df.empty:
            return pd.DataFrame()

        # 時間範圍過濾（回測用）
        if start:
            df = df[df.index >= pd.Timestamp(start, tz="UTC")]
        if end:
            df = df[df.index <= pd.Timestamp(end, tz="UTC")]

        # 取最新 N 根
        if limit:
            df = df.tail(limit)

        return df.copy()

    def load_all_timeframes(self, contract_id: str) -> Dict[str, pd.DataFrame]:
        """一次讀取所有時間框，供 SR 多時框分析使用"""
        result = {}
        for tf, cfg in TIMEFRAME_CONFIG.items():
            df = self.load(contract_id, tf)
            if not df.empty:
                result[tf] = df
                logger.debug(f"[BarStore] 載入 {cfg['label']} {len(df)} 根")
        return result

    # ──────────────────────────────────────────
    # 統計資訊
    # ──────────────────────────────────────────

    def summary(self, contract_id: str) -> str:
        lines = [f"[BarStore] 數據摘要 | {contract_id}"]
        for tf, cfg in TIMEFRAME_CONFIG.items():
            df = self.load(contract_id, tf)
            if df.empty:
                lines.append(f"  {cfg['label']:8s} → 無數據")
            else:
                start_str = df.index.min().strftime("%Y-%m-%d")
                end_str   = df.index.max().strftime("%Y-%m-%d %H:%M")
                size_kb   = self._path(contract_id, tf).stat().st_size // 1024 \
                            if self._path(contract_id, tf).exists() else 0
                lines.append(
                    f"  {cfg['label']:8s} → {len(df):5d} 根 "
                    f"| {start_str} ~ {end_str} | {size_kb} KB"
                )
        return "\n".join(lines)

    # ──────────────────────────────────────────
    # 內部工具
    # ──────────────────────────────────────────

    def _path(self, contract_id: str, tf: str) -> Path:
        safe_id = contract_id.replace(".", "_").replace("/", "_")
        return self.data_dir / f"{safe_id}_{tf}.parquet"

    def _read_parquet(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_parquet(path, engine="pyarrow")
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            logger.error(f"[BarStore] 讀取失敗 {path}: {e}")
            return pd.DataFrame()

    def _write_parquet(self, df: pd.DataFrame, path: Path):
        try:
            df = df.copy()
            # 確保 index 是 DatetimeIndex 且有時區
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif not hasattr(df.index, 'tz') or df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.to_parquet(path, engine="pyarrow", compression="snappy")
        except Exception as e:
            logger.error(f"[BarStore] 寫入失敗 {path}: {e}")

    def _merge(self, old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        """合併兩個 DataFrame，去重並排序"""
        merged = pd.concat([old_df, new_df])
        merged = merged[~merged.index.duplicated(keep="last")]
        merged.sort_index(inplace=True)
        return merged

    def _update_cache(self, contract_id: str, tf: str, df: pd.DataFrame):
        self._cache[f"{contract_id}_{tf}"] = df

    async def _fetch_range(self, contract_id: str, cfg: dict,
                           start: datetime, end: datetime) -> pd.DataFrame:
        """
        從 API 拉取指定時間範圍的 K 棒。
        單次最多 20000 根，超過時自動分批拉取。
        """
        all_dfs = []
        # 統一轉為 naive UTC，避免 offset-naive vs offset-aware 比較錯誤
        if hasattr(start, 'tzinfo') and start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        if hasattr(end, 'tzinfo') and end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        batch_start = start
        max_per_batch = 5000  # 保守值，避免超過 API 上限

        while batch_start < end:
            try:
                df = await self.ms.get_bars_df(
                    contract_id,
                    unit=cfg["unit"],
                    unit_number=cfg["unit_number"],
                    limit=max_per_batch,
                    start_time=batch_start,
                    end_time=end
                )
                if df.empty:
                    break

                all_dfs.append(df)

                # 下一批從這批最後一根的下一個時間點開始
                last_ts = df.index.max().to_pydatetime().replace(tzinfo=None)
                if last_ts <= batch_start:
                    break  # 防止無限迴圈
                batch_start = last_ts + timedelta(minutes=cfg["unit_number"]
                    if cfg["unit"] == BarUnit.MINUTE else
                    cfg["unit_number"] * 60 if cfg["unit"] == BarUnit.HOUR else
                    cfg["unit_number"] * 1440)

                if len(df) < max_per_batch:
                    break  # 已取到最新，不需要再拉

                await asyncio.sleep(0.5)  # 避免頻率限制

            except Exception as e:
                logger.error(f"[BarStore] API 拉取失敗: {e}")
                break

        if not all_dfs:
            return pd.DataFrame()

        result = pd.concat(all_dfs)
        result = result[~result.index.duplicated(keep="last")]
        result.sort_index(inplace=True)
        return result
